"""Corridor-reservation deferral window is exception-safe (Issue #3489).

Follow-up to #3488.  ``Autorouter.route_all_negotiated`` opens an
iteration-scoped C++ unmark-deferral window
(``RoutingGrid.begin_cpp_unmark_deferral``) at the rip-up site and
relies on a post-loop flush (``_flush_corridor_reservation``) to close
it.  Before #3489 there was no ``try/finally`` around the iteration
loop: an unexpected exception escaping ``route_all_negotiated`` while a
window was open left the grid with ``_cpp_unmark_deferred == True``.

On a long-lived grid (library / multi-route flows) that stuck flag
makes every subsequent ``unmark_route`` silently QUEUE its C++ mirror
instead of applying it, diverging the Python and C++ grids until the
next ``route_all_negotiated`` call self-heals at its first iteration
top.

These tests pin the contract that the flag is reset to ``False`` on
EVERY exit path -- normal completion AND an escaped exception -- so the
divergence window can never open.
"""

from __future__ import annotations

import pytest

import kicad_tools.router.core as core
from kicad_tools.router.core import Autorouter


def _build_trivial_router() -> Autorouter:
    """Two crossing single-net component pairs on a 20x20mm board.

    Mirrors the fixture used by the negotiated-loop regression tests in
    ``tests/test_best_metric_patience.py`` -- both nets route trivially,
    so ``route_all_negotiated`` enters the iteration loop and runs the
    unconditional top-of-iteration bookkeeping before converging.
    """
    router = Autorouter(width=20.0, height=20.0)
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 18.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ],
    )
    router.add_component(
        "R2",
        [
            {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
        ],
    )
    return router


def test_exception_inside_open_window_resets_deferral_flag(monkeypatch):
    """An exception raised while a deferral window is open must leave the
    grid with ``_cpp_unmark_deferred == False`` after
    ``route_all_negotiated`` unwinds (Issue #3489 acceptance criterion).

    We inject the failure at ``should_terminate_early`` -- a function
    called ONLY from inside the negotiated iteration loop (the
    rip-up/reroute body), and inside the ``try`` whose ``finally`` must
    close the window.  On its first invocation we open a real deferral
    window and then raise, simulating an unexpected exception escaping
    the loop with a window still open.

    ``get_total_overflow`` is forced positive so the initial pass does
    not declare convergence and short-circuit before the iteration loop;
    that guarantees the loop is entered and the injection point is
    reached.
    """
    router = _build_trivial_router()
    grid = router.grid

    # Force the negotiated loop to run: a non-zero overflow defeats the
    # "no conflicts - routing complete" early return after the initial
    # pass, so ``route_all_negotiated`` enters the rip-up/reroute loop.
    monkeypatch.setattr(grid, "get_total_overflow", lambda: 1)

    state = {"raised": False}

    def _open_window_then_raise(*_args, **_kwargs):
        if not state["raised"]:
            state["raised"] = True
            # Simulate the rip-up site opening the deferral window...
            grid.begin_cpp_unmark_deferral()
            assert grid._cpp_unmark_deferred is True
            # ...and an unexpected exception escaping mid-window.
            raise RuntimeError("injected fault inside deferral window")
        return False

    # ``should_terminate_early`` is referenced via the module namespace
    # inside ``route_all_negotiated`` (``adaptive=True`` reaches it), and
    # ONLY from within the iteration loop -- exactly the in-window site
    # the fix must guard.
    monkeypatch.setattr(core, "should_terminate_early", _open_window_then_raise)

    with pytest.raises(RuntimeError, match="injected fault inside deferral window"):
        router.route_all_negotiated(
            max_iterations=3,
            timeout=10.0,
            adaptive=True,
            perturbation=False,
        )

    # The fault must actually have fired inside an open window.
    assert state["raised"] is True
    # The try/finally guard must have closed the window despite the
    # escaped exception -- this is the core regression assertion.  Use
    # getattr because the flush leaves the attribute defined as False.
    assert getattr(grid, "_cpp_unmark_deferred", False) is False
    assert getattr(grid, "_deferred_cpp_unmarks", []) == []


def test_normal_completion_leaves_deferral_flag_reset():
    """The non-raising path is unchanged: a clean ``route_all_negotiated``
    run leaves the grid with ``_cpp_unmark_deferred == False`` (the flush
    in ``finally`` is a no-op when no window is open, so normal behaviour
    is preserved -- Issue #3489 AC: no behaviour change on the normal
    path).
    """
    router = _build_trivial_router()

    routes = router.route_all_negotiated(
        max_iterations=3,
        timeout=10.0,
        adaptive=False,
        perturbation=False,
    )

    assert isinstance(routes, list)
    assert getattr(router.grid, "_cpp_unmark_deferred", False) is False
    assert getattr(router.grid, "_deferred_cpp_unmarks", []) == []
