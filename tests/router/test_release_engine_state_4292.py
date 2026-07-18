"""Guard tests for the pre-tail engine-state release (issue #4292).

The ``kct route`` CLI tail (zone fill + internal DRC) shells out to
``kicad-cli`` -- the dominant memory consumer of the whole run -- and never
reads the routing grid or the lattice negotiation structures again.  Issue
#4292 releases those before the tail so their resident footprint does not
coexist with the ``kicad-cli`` child.  These tests lock in the two invariants
that make the release safe:

1. the heavy per-cell arrays are actually dropped, while the grid metadata
   the post-route diagnostics read survives, and
2. the release only touches the negotiation scratch -- the committed routes,
   pad/net topology, and net-class map (all read by the tail) are preserved.
"""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.cli.route_cmd import _release_routing_engine_state
from kicad_tools.router import DesignRules
from kicad_tools.router.grid import RoutingGrid


def _make_grid() -> RoutingGrid:
    rules = DesignRules(
        trace_width=0.127,
        trace_clearance=0.127,
        grid_resolution=0.1,
        min_trace_width=0.127,
    )
    return RoutingGrid(width=20.0, height=20.0, rules=rules)


def test_release_arrays_drops_cells_keeps_metadata():
    """RoutingGrid.release_arrays frees the dense planes but keeps metadata."""
    grid = _make_grid()
    # Sanity: the occupancy planes are populated before release.
    assert grid._blocked.size > 0
    assert grid._net.size > 0
    assert grid._congestion.size > 0

    res, layers, cols, rows = grid.resolution, grid.num_layers, grid.cols, grid.rows

    grid.release_arrays()

    # Dense arrays are freed (zero-length), but remain ndarrays.
    assert grid._blocked.size == 0
    assert grid._net.size == 0
    assert grid._usage_count.size == 0
    assert grid._history_cost.size == 0
    assert grid._is_obstacle.size == 0
    assert grid._is_zone.size == 0
    assert grid._pad_blocked.size == 0
    assert grid._original_net.size == 0
    assert grid._congestion.size == 0
    assert grid._clearance_masks == {}
    assert grid._present_cost_ema is None

    # Metadata the diagnostics read is untouched.
    assert grid.resolution == res
    assert grid.num_layers == layers
    assert grid.cols == cols
    assert grid.rows == rows

    # Idempotent: a second call must not raise.
    grid.release_arrays()


def test_release_engine_state_preserves_topology():
    """The helper drops grid/lattice scratch but preserves tail-read state."""
    grid = _make_grid()
    routes = ["route-a", "route-b"]
    pads = {("R1", "1"): object()}
    nets = {1: [("R1", "1")]}
    net_names = {1: "GND"}
    net_class_map = {"GND": object()}

    router = SimpleNamespace(
        grid=grid,
        routes=routes,
        pads=pads,
        nets=nets,
        net_names=net_names,
        net_class_map=net_class_map,
        _lattice_pathfinder=object(),
        _lattice_net_routes={1: ["r"]},
    )

    _release_routing_engine_state(router)

    # Negotiation scratch is released.
    assert router._lattice_pathfinder is None
    assert router._lattice_net_routes is None
    assert router.grid._blocked.size == 0

    # Everything the tail still consumes is preserved by identity.
    assert router.routes is routes
    assert router.pads is pads
    assert router.nets is nets
    assert router.net_names is net_names
    assert router.net_class_map is net_class_map
    # The grid object itself survives (diagnostics read its metadata).
    assert router.grid is grid


def test_release_engine_state_is_best_effort():
    """Absent attributes are skipped without raising (older / fixture routers)."""
    router = SimpleNamespace(routes=[], pads={}, nets={})
    # No grid, no lattice attributes -- must be a safe no-op.
    _release_routing_engine_state(router)
    assert router.routes == []
