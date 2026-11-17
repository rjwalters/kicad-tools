"""The resumable A*'s cross-node via-drop verdict cache (Issue #5617).

``Pathfinder::run_astar_loop`` memoises ``is_via_blocked_diag`` on ``(x, y)``
for the duration of ONE loop call: a node at ``(x, y, L1)`` asks the same
full-stack question as ``(x, y, L0)``.  The cache must never outlive the call
-- Python can change the grid between ``route_resumable()`` / ``resume()``
calls on the same ``Pathfinder``.  These tests pin that invalidation and the
replayed blocker diagnostics.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native router required")

X, Y = 5.0, 5.0
NET, FOREIGN = 1, 2


def _setup():
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(10.0, 10.0, rules, layer_stack=LayerStack.four_layer_all_signal())
    native = CppGrid.from_routing_grid(grid)
    router = CppPathfinder(native, rules)
    router.set_routable_layers(native.get_routable_indices())
    return rules, native, router


def _route(router):
    # Same (x, y), different layers: the only way to the goal is a via.
    return router._impl.route_resumable(X, Y, 0, X, Y, 3, NET, max_search_iterations=200_000)


def _via_gap(via, sx, sy, diameter):
    return math.hypot(via.x - sx, via.y - sy) - (via.diameter + diameter) / 2


def test_fresh_search_does_not_reuse_previous_call_verdicts():
    rules, native, router = _setup()
    first = _route(router)
    assert first.success and first.vias
    v = first.vias[0]

    # A foreign buried (In1-In2) via exactly where the first search dropped
    # its via: the outer-layer trace steps stay legal, the through-via drop
    # there does not.
    native._impl.add_stored_via(
        v.x, v.y, rules.via_drill, rules.via_diameter, FOREIGN, layer_from=1, layer_to=2
    )

    second = _route(router)
    assert second.success and second.vias
    for via in second.vias:
        assert _via_gap(via, v.x, v.y, rules.via_diameter) >= rules.via_clearance - 1e-4


def test_replayed_verdict_keeps_via_via_diagnostics():
    """Every target layer of every node still reports the via-via blocker."""
    rules, native, router = _setup()
    # A carpet of In1..B.Cu foreign vias: F.Cu motion stays legal, every via
    # drop is geometrically blocked (same fixture shape as Issue #2476's).
    for i in range(26):
        for j in range(26):
            native._impl.add_stored_via(
                i * 0.4,
                j * 0.4,
                rules.via_drill,
                rules.via_diameter,
                FOREIGN,
                layer_from=1,
                layer_to=3,
            )
    result = router._impl.route_resumable(X, Y, 0, X + 1.0, Y, 3, NET, max_search_iterations=20_000)
    assert not result.success
    assert result.blocking_via_net == FOREIGN
