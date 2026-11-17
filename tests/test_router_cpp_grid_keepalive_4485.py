"""Regression tests for issue #4485.

``tests/test_router_cpp_padbounds_leak_4346.py::
test_omitted_pad_bounds_matches_explicit_all_zero`` failed intermittently in
CI with nondeterministic segment counts (``assert 2 == 0``, ``assert 98 ==
90``, ...).  AddressSanitizer traced this to a **heap-use-after-free** inside
``Pathfinder::route`` at ``grid_.at(...)``: the C++ ``Pathfinder`` stores a
bare ``Grid3D& grid_`` reference, but the nanobind constructor binding lacked a
``keep_alive`` policy.  In the common ``_make_pathfinder().route(...)`` idiom
the ``Grid3D`` Python wrapper is an unnamed temporary whose last reference is
dropped as soon as the factory returns, so nanobind garbage-collects it and
frees the underlying ``cells_`` storage -- leaving ``grid_`` dangling.  The
next ``route()`` then reads freed heap, and whatever happens to occupy that
memory (allocator- and load-dependent) steers the A* search down different
paths, hence the run-to-run instability.

The fix adds ``nb::keep_alive<1, 2>()`` to the ``Pathfinder`` and
``CoupledPathfinder`` constructor bindings, tying the grid argument's lifetime
to the constructed pathfinder.  These tests assert that policy directly (the
grid's Python reference count is elevated by the pathfinder holding a strong
reference) and behaviourally (routing remains valid and deterministic after
every external reference to the grid has been dropped and collected).
"""

import gc
import sys

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not built (run `kct build-native`)",
)


def _make_rules():
    from kicad_tools.router import router_cpp

    rules = router_cpp.DesignRules()
    rules.trace_width = 0.2
    rules.trace_clearance = 0.2
    rules.via_diameter = 0.6
    rules.via_drill = 0.3
    rules.via_clearance = 0.2
    rules.grid_resolution = 0.5
    return rules


_ROUTE_KWARGS = {
    "start_x": 5.0 * 0.5,
    "start_y": 50.0 * 0.5,
    "start_layer": 0,
    "end_x": 95.0 * 0.5,
    "end_y": 50.0 * 0.5,
    "end_layer": 0,
    "net": 1,
}


def test_pathfinder_keeps_grid_alive() -> None:
    """Constructing a ``Pathfinder`` must hold a strong reference to its grid.

    Without ``nb::keep_alive<1, 2>()`` the pathfinder stores only a bare C++
    ``Grid3D&`` and does NOT bump the Python refcount, so the grid can be
    freed out from under the still-live pathfinder (issue #4485).  A refcount
    delta of at least one proves the keep-alive edge exists.
    """
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(100, 100, 2, 0.5, 0.0, 0.0)
    before = sys.getrefcount(grid)
    pathfinder = router_cpp.Pathfinder(grid, _make_rules(), True)
    after = sys.getrefcount(grid)

    assert after > before, (
        "Pathfinder did not take a strong reference to its Grid3D -- the "
        "nb::keep_alive<1, 2> policy on the constructor binding has regressed "
        f"(refcount {before} -> {after}, issue #4485)."
    )
    # Keep ``pathfinder`` live until after the assertion.
    assert pathfinder is not None


def test_coupled_pathfinder_keeps_grid_alive() -> None:
    """``CoupledPathfinder`` has the same bare ``Grid3D&`` member and must
    likewise keep its grid alive (issue #4485)."""
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(100, 100, 2, 0.5, 0.0, 0.0)
    before = sys.getrefcount(grid)
    coupled = router_cpp.CoupledPathfinder(grid, _make_rules(), 4, 2, 1, 0, 1, 1.0, 1.0)
    after = sys.getrefcount(grid)

    assert after > before, (
        "CoupledPathfinder did not take a strong reference to its Grid3D -- "
        "the nb::keep_alive<1, 2> policy has regressed (refcount "
        f"{before} -> {after}, issue #4485)."
    )
    assert coupled is not None


def test_route_survives_dropped_grid_reference() -> None:
    """Routing must stay valid after every external grid reference is dropped.

    This reproduces the exact ``_make_pathfinder().route(...)`` shape from the
    #4485 failure: the ``Grid3D`` is an unnamed temporary owned only by the
    pathfinder.  We force a collection between construction and routing (and
    churn the allocator) so that, absent the keep-alive fix, the freed grid
    storage would very likely be reused -- corrupting the A* search.  With the
    fix the grid stays alive and the route is well-formed.
    """
    from kicad_tools.router import router_cpp

    def make_pathfinder():
        # The Grid3D temporary has NO surviving Python name once this
        # function returns -- only the Pathfinder's keep-alive edge holds it.
        return router_cpp.Pathfinder(
            router_cpp.Grid3D(100, 100, 2, 0.5, 0.0, 0.0), _make_rules(), True
        )

    pathfinder = make_pathfinder()

    # Aggressively encourage reuse of any freed grid storage.
    gc.collect()
    _churn = [bytearray(4096) for _ in range(256)]
    del _churn
    gc.collect()

    result = pathfinder.route(**_ROUTE_KWARGS)
    assert result.success, "route on a pathfinder whose grid temporary was dropped failed"
    assert len(result.segments) > 0


def test_dropped_grid_route_matches_retained_grid_route() -> None:
    """A route whose grid was dropped must match one whose grid is retained.

    Establishes the equivalence the original #4485 test asserted, but framed as
    the actual invariant at stake: dropping the caller's grid reference must
    NOT change routing.  Run repeatedly to catch any residual nondeterminism.
    """
    from kicad_tools.router import router_cpp

    # Reference: keep an explicit local name on the grid for its whole life.
    retained_grid = router_cpp.Grid3D(100, 100, 2, 0.5, 0.0, 0.0)
    retained_pf = router_cpp.Pathfinder(retained_grid, _make_rules(), True)
    reference = retained_pf.route(**_ROUTE_KWARGS)
    assert reference.success

    for _ in range(25):
        # Grid is a pure temporary held only by the pathfinder.
        pf = router_cpp.Pathfinder(
            router_cpp.Grid3D(100, 100, 2, 0.5, 0.0, 0.0), _make_rules(), True
        )
        gc.collect()
        result = pf.route(**_ROUTE_KWARGS)
        assert result.success
        assert len(result.segments) == len(reference.segments)
        assert len(result.vias) == len(reference.vias)
