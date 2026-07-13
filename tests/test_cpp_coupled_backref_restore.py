"""Regression tests for Issue #4077.

``CoupledPathfinder._get_cpp_coupled_impl`` snapshots the single-ended
router's ``grid._cpp_grid`` back-reference before calling
``CppGrid.from_routing_grid(self.grid)`` (which hijacks that back-reference
at ``cpp_backend.py:559``) and restores it afterwards.

Issue #4065 established that the restore must run before the
``CppCoupledPathfinder(...)`` constructor -- the constructor-throws case was
already covered.  Issue #4077 hardens the restore into a ``try/finally`` so
the back-reference is also restored when ``from_routing_grid`` raises AFTER
it has already reassigned ``grid._cpp_grid`` (a mid-copy failure, e.g. during
the bulk cell copy or the Issue #4071 corridor-reservation marshalling).

These tests monkeypatch ``CppGrid.from_routing_grid`` to raise partway
through -- after the hijack -- and assert the single-ended router's
back-reference is restored to its pre-call value, not left pointing at the
partially-built coupled grid.
"""

from __future__ import annotations

import pytest

from kicad_tools.router import cpp_backend
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ coupled back-reference restore requires the router_cpp backend (kct build-native)",
)


def _make_pf() -> CoupledPathfinder:
    grid = RoutingGrid(width=12.7, height=12.7, rules=DesignRules())
    pf = CoupledPathfinder(
        grid=grid, rules=DesignRules(), target_spacing_cells=2, min_spacing_cells=2
    )
    pf._use_cpp_coupled = True
    return pf


class _Sentinel:
    """Stand-in for the partially-built coupled CppGrid."""


def test_backref_restored_when_from_routing_grid_raises_after_hijack(monkeypatch):
    """A mid-copy failure (raises AFTER assigning grid._cpp_grid) must still
    restore the single-ended router's back-reference.

    This is the case Issue #4077 targets: prior to the fix the restore was a
    plain statement inside the ``try`` that was skipped when
    ``from_routing_grid`` raised after line 559, leaving ``grid._cpp_grid``
    pointing at the partial coupled grid.
    """
    pf = _make_pf()

    # The single-ended router's back-reference is unset on a fresh grid.
    assert getattr(pf.grid, "_cpp_grid", None) is None

    hijacked = _Sentinel()

    def _boom(_grid):
        # Simulate cpp_backend.py:559: from_routing_grid reassigns the
        # Python grid's back-reference to the (partially-built) coupled grid...
        _grid._cpp_grid = hijacked
        # ...then raises before finishing the copy (bulk cell copy / #4071
        # corridor-reservation marshalling on a malformed grid).
        raise RuntimeError("mid-copy failure after grid._cpp_grid was set")

    monkeypatch.setattr(cpp_backend.CppGrid, "from_routing_grid", classmethod(_boom))

    impl = pf._get_cpp_coupled_impl()

    # Outer try/except routes the failure to the Python fallback.
    assert impl is None
    assert pf._use_cpp_coupled is False
    # The back-reference must be restored to its pre-call value (None here),
    # NOT left pointing at the partially-built coupled grid.
    assert getattr(pf.grid, "_cpp_grid", None) is not hijacked
    assert getattr(pf.grid, "_cpp_grid", None) is None


def test_backref_restored_to_prior_value_when_previously_set(monkeypatch):
    """When the grid already had a back-reference, the finally must restore
    that exact prior object (not None), even on a mid-copy failure."""
    pf = _make_pf()

    prior = _Sentinel()
    pf.grid._cpp_grid = prior

    hijacked = _Sentinel()

    def _boom(_grid):
        _grid._cpp_grid = hijacked
        raise RuntimeError("mid-copy failure after grid._cpp_grid was set")

    monkeypatch.setattr(cpp_backend.CppGrid, "from_routing_grid", classmethod(_boom))

    impl = pf._get_cpp_coupled_impl()

    assert impl is None
    assert pf._use_cpp_coupled is False
    # Restored to the exact prior back-reference, not the hijacked partial grid.
    assert pf.grid._cpp_grid is prior


def test_happy_path_restores_backref_and_caches_impl():
    """When from_routing_grid and the constructor both succeed, the
    single-ended router's back-reference is still restored (not left pointing
    at the coupled grid) and the impl is cached."""
    pf = _make_pf()
    assert getattr(pf.grid, "_cpp_grid", None) is None

    impl = pf._get_cpp_coupled_impl()

    assert impl is not None
    assert pf._cpp_coupled_impl is impl
    assert pf._cpp_coupled_grid is pf.grid
    # The coupled build must NOT hijack the single-ended back-reference: it is
    # restored to its pre-call value (None) after from_routing_grid ran.
    assert getattr(pf.grid, "_cpp_grid", None) is None

    # Cached: a second call returns the same impl without rebuilding.
    assert pf._get_cpp_coupled_impl() is impl
