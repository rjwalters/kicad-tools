"""Issue #6217: ``--per-net-iterations`` must reach the pure-Python backend.

Before #6217 ``create_hybrid_router(force_python=True, per_net_iterations=N)``
returned a bare :class:`~kicad_tools.router.pathfinder.Router` and dropped
``N``: the tuned per-net cap (#3881) only reached the Python A* when it ran
as the C++ wrapper's fallback.  ``--backend python --deterministic-budget``
therefore had no per-net bound at all (the normalizer zeroes
``--per-net-timeout``), and ``--backend python`` routing stayed
wall-clock-dependent whatever the user asked for.
"""

from __future__ import annotations

from kicad_tools.router.cpp_backend import create_hybrid_router
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _grid() -> tuple[RoutingGrid, DesignRules]:
    rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
    return RoutingGrid(width=10.0, height=10.0, rules=rules), rules


def test_python_backend_applies_per_net_iteration_cap() -> None:
    grid, rules = _grid()
    router = create_hybrid_router(grid, rules, force_python=True, per_net_iterations=1234)
    assert router._max_iterations_override == 1234


def test_python_backend_without_cap_keeps_historical_bound() -> None:
    grid, rules = _grid()
    router = create_hybrid_router(grid, rules, force_python=True)
    assert router._max_iterations_override is None


def test_cap_bounds_a_python_search() -> None:
    """A cap far below what the route needs makes the search give up."""
    grid, rules = _grid()
    start = Pad(x=1.0, y=1.0, width=0.5, height=0.5, net=1, net_name="N1", ref="U1", pin="1")
    end = Pad(x=9.0, y=9.0, width=0.5, height=0.5, net=1, net_name="N1", ref="U2", pin="1")
    for pad in (start, end):
        grid.add_pad(pad)

    uncapped = create_hybrid_router(grid, rules, force_python=True)
    assert uncapped.route(start, end) is not None

    capped = create_hybrid_router(grid, rules, force_python=True, per_net_iterations=5)
    assert capped.route(start, end) is None
