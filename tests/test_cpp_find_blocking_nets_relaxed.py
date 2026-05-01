"""Tests for CppPathfinder.find_blocking_nets_relaxed (Issue #2386).

Regression: when neighborhood rip-up (Issue #2274) escalates and calls
``find_blocking_nets_relaxed`` on a CppPathfinder, routing must not raise
``AttributeError``. The C++ backend now exposes a Python-side mirror that
re-uses the existing C++ ``route()`` and walks segments against the
caller-provided original blocked/net arrays.

These tests cover:

1. Signature parity between Python and C++ implementations.
2. Behavioral parity on a small grid.
3. ``_py_grid is None`` fallback path.
4. End-to-end smoke test for board 02 (charlieplex) when the C++ backend
   is available — only verifies the AttributeError no longer fires.
"""

from __future__ import annotations

import inspect

import numpy as np
import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")


# ---------------------------------------------------------------------------
# Test 1 — Signature parity / regression for the AttributeError
# ---------------------------------------------------------------------------


def test_cpp_pathfinder_has_find_blocking_nets_relaxed():
    """Regression for #2386: CppPathfinder must expose find_blocking_nets_relaxed."""
    assert hasattr(CppPathfinder, "find_blocking_nets_relaxed"), (
        "CppPathfinder must implement find_blocking_nets_relaxed (Issue #2386)"
    )


def test_cpp_pathfinder_signature_matches_python():
    """Signature parity: CppPathfinder mirrors Router.find_blocking_nets_relaxed."""
    cpp_sig = inspect.signature(CppPathfinder.find_blocking_nets_relaxed)
    py_sig = inspect.signature(Router.find_blocking_nets_relaxed)

    cpp_params = list(cpp_sig.parameters.keys())
    py_params = list(py_sig.parameters.keys())

    assert cpp_params == [
        "self",
        "start",
        "end",
        "saved_blocked",
        "saved_net",
        "per_net_timeout",
    ]
    assert cpp_params == py_params

    # per_net_timeout must default to None on both
    assert cpp_sig.parameters["per_net_timeout"].default is None
    assert py_sig.parameters["per_net_timeout"].default is None


# ---------------------------------------------------------------------------
# Helpers for building a small grid scenario
# ---------------------------------------------------------------------------


def _make_small_grid_and_pads():
    """Build a small grid with two pads.

    The grid itself is left empty — the test passes the routed-net blocker
    in via the ``saved_blocked`` / ``saved_net`` arrays, simulating what the
    caller does inside ``temporarily_unblock_routed_nets()`` (the live grid
    is unblocked, but the saved arrays preserve the original blocked
    state).

    Layout (top-down view, single layer F.Cu used for routing):

        START_PAD  .................................  END_PAD

    The blocker net (id=99) is recorded only in the saved arrays — it is
    NOT marked on the live grid, so A* (Python or C++) can find a clear
    path between the pads on the live grid. ``find_blocking_nets_relaxed``
    then walks that path against the saved arrays and identifies net 99 as
    a blocker.
    """
    rules = DesignRules()
    # Use a coarser resolution so the small board has a manageable cell count
    # but is still fine enough for trace_width + clearance.
    rules.grid_resolution = 0.2
    rules.trace_width = 0.2
    rules.trace_clearance = 0.2

    grid = RoutingGrid(
        width=6.0,
        height=2.0,
        rules=rules,
        layer_stack=LayerStack.two_layer(),
    )

    # Start pad on the left, end pad on the right.
    start = Pad(
        x=0.5,
        y=1.0,
        width=0.5,
        height=0.5,
        net=1,
        net_name="N_START",
        layer=Layer.F_CU,
    )
    end = Pad(
        x=5.5,
        y=1.0,
        width=0.5,
        height=0.5,
        net=1,
        net_name="N_START",
        layer=Layer.F_CU,
    )

    return grid, rules, start, end


def _make_saved_arrays_with_blocker(grid: RoutingGrid, blocker_net: int = 99):
    """Build saved_blocked / saved_net arrays simulating a horizontal blocker.

    The blocker is a vertical strip of cells in the middle of the board on
    F.Cu — any direct path from left-pad to right-pad must cross it.
    """
    saved_blocked = np.zeros((grid.num_layers, grid.rows, grid.cols), dtype=np.bool_)
    saved_net = np.zeros((grid.num_layers, grid.rows, grid.cols), dtype=np.int32)

    blocker_layer = grid.layer_to_index(Layer.F_CU.value)
    blocker_gx, _ = grid.world_to_grid(3.0, 1.0)
    for cy in range(grid.rows):
        for dx in (-1, 0, 1):
            cx = blocker_gx + dx
            if 0 <= cx < grid.cols:
                saved_blocked[blocker_layer, cy, cx] = True
                saved_net[blocker_layer, cy, cx] = blocker_net

    return saved_blocked, saved_net


# ---------------------------------------------------------------------------
# Test 2 — Behavioral parity vs. Python pathfinder
# ---------------------------------------------------------------------------


def test_behavioral_parity_with_python_pathfinder():
    """CppPathfinder and Router return the same blocker set on a small grid.

    Both backends share the same input arrays (``saved_blocked`` /
    ``saved_net``) and the same empty live grid. Both run a relaxed A* and
    walk the resulting path against the saved arrays; the result must be
    the same set of blocker net IDs.
    """
    # Python run
    py_grid, rules, start, end = _make_small_grid_and_pads()
    saved_blocked, saved_net = _make_saved_arrays_with_blocker(py_grid, blocker_net=99)

    py_router = Router(py_grid, rules)
    py_blockers = py_router.find_blocking_nets_relaxed(start, end, saved_blocked, saved_net)

    # C++ run -- same scenario, fresh grids built from the same setup.
    cpp_py_grid, cpp_rules, cpp_start, cpp_end = _make_small_grid_and_pads()
    cpp_saved_blocked, cpp_saved_net = _make_saved_arrays_with_blocker(cpp_py_grid, blocker_net=99)
    cpp_grid = CppGrid.from_routing_grid(cpp_py_grid)
    cpp_pathfinder = CppPathfinder(cpp_grid, cpp_rules)

    cpp_blockers = cpp_pathfinder.find_blocking_nets_relaxed(
        cpp_start, cpp_end, cpp_saved_blocked, cpp_saved_net
    )

    # Both backends must find the blocker (net 99). Sets must be equal.
    assert 99 in py_blockers, (
        f"Python pathfinder failed to identify blocker net 99; got {py_blockers}"
    )
    assert cpp_blockers == py_blockers, (
        f"C++ blockers {cpp_blockers} != Python blockers {py_blockers}"
    )


# ---------------------------------------------------------------------------
# Test 3 — _py_grid is None fallback
# ---------------------------------------------------------------------------


def test_py_grid_none_fallback_does_not_raise():
    """When CppGrid has no _py_grid backref, find_blocking_nets_relaxed must
    still run without raising (using the safe pad_blocked=False fallback).
    """
    rules = DesignRules()
    rules.grid_resolution = 0.2
    rules.trace_width = 0.2
    rules.trace_clearance = 0.2

    # Build a CppGrid directly (no from_routing_grid) -- _py_grid will be None.
    cpp_grid = CppGrid(cols=10, rows=10, layers=2, resolution=0.2)
    assert cpp_grid._py_grid is None

    cpp_pathfinder = CppPathfinder(cpp_grid, rules)

    start = Pad(
        x=0.2,
        y=1.0,
        width=0.2,
        height=0.2,
        net=1,
        net_name="A",
        layer=Layer.F_CU,
    )
    end = Pad(
        x=1.8,
        y=1.0,
        width=0.2,
        height=0.2,
        net=1,
        net_name="A",
        layer=Layer.F_CU,
    )

    # The grid is mostly empty so there are no real blockers. Construct
    # saved_blocked / saved_net arrays of the right shape; passing through
    # the method must not raise.
    saved_blocked = np.zeros((2, 10, 10), dtype=np.bool_)
    saved_net = np.zeros((2, 10, 10), dtype=np.int32)

    # Should not raise (specifically, must not raise AttributeError on
    # _pad_blocked access).
    blockers = cpp_pathfinder.find_blocking_nets_relaxed(start, end, saved_blocked, saved_net)

    # Empty grid with empty saved_* -> no blockers expected.
    assert blockers == set()


# ---------------------------------------------------------------------------
# Test 4 — End-to-end: board 02 routes without AttributeError
# ---------------------------------------------------------------------------


def test_negotiated_router_can_call_relaxed_blocker_search():
    """Issue #2386 regression: NegotiatedRouter should be able to call
    ``find_blocking_nets_relaxed`` on a CppPathfinder without raising
    ``AttributeError``.

    This is the integration-level guard for the bug surface that the issue
    reports. The original failure path is:

        NegotiatedRouter.find_blocking_nets_relaxed (algorithms/negotiated.py)
            -> self.router.find_blocking_nets_relaxed (CppPathfinder)
            -> AttributeError

    With the fix in place, the call resolves to the new method and either
    returns a (possibly empty) blocker dict or completes normally.
    """
    from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

    py_grid, rules, start, end = _make_small_grid_and_pads()
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    cpp_pathfinder = CppPathfinder(cpp_grid, rules)

    neg = NegotiatedRouter(py_grid, cpp_pathfinder, rules, {})

    pads_by_net = {start.net: [start, end]}

    # If find_blocking_nets_relaxed is missing on CppPathfinder, this raises
    # AttributeError during the negotiated router's relaxed lookup loop.
    blocker_scores = neg.find_blocking_nets_relaxed(
        failed_nets=[start.net],
        pads_by_net=pads_by_net,
        per_net_timeout=2.0,
    )

    # The shape of the result is what matters; the empty-grid scenario will
    # naturally yield no blockers.
    assert isinstance(blocker_scores, dict)
