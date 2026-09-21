"""Bounding-box pruning in ``RouteHaloGeometry.clear`` must be verdict-exact.

Issue #5240: ``clear`` was the largest pure-Python cost in the board 06
re-route -- a Sept-17 ``cProfile`` capture of a full board 06 regeneration
attributed 57.1 M of the run's 62.4 M ``shapely`` ``distance`` calls to this
one method.  The optimization rejects a nearby object with a precomputed
axis-aligned bounding-box gap before paying for the exact GEOS distance.

The gap between two bounding boxes is a lower bound on the distance between
the geometries they contain, so a candidate whose box gap already exceeds
``margin + other_radius`` can never reach either ``return False`` branch.
These tests pin that claim by running the same corpus twice -- once with the
prune enabled, once with ``_PRUNE_BY_BOUNDS`` disabled so the old exhaustive
path runs -- and asserting the verdicts match exactly.
"""

from __future__ import annotations

import random

import pytest
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]

from kicad_tools.router import route_halo_geometry
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

LAYERS = [Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU]
NET_NAMES = {1: "N1", 2: "N2", 3: "N3", 4: "N4"}


def _context(seed: int, *, pairwise: bool):
    """A populated 20x20 mm board: four nets of traces plus through vias."""
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        min_drill_clearance=0.25,
        grid_resolution=0.127,
    )
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    rng = random.Random(seed)
    for net, name in NET_NAMES.items():
        route = Route(net=net, net_name=name)
        for _ in range(6):
            x, y = rng.uniform(5.0, 15.0), rng.uniform(5.0, 15.0)
            dx, dy = rng.choice([(1.0, 0.0), (0.0, 1.0), (0.8, 0.8), (0.8, -0.8)])
            route.segments.append(
                Segment(
                    x,
                    y,
                    x + dx,
                    y + dy,
                    rng.choice([0.15, 0.2, 0.25]),
                    rng.choice(LAYERS),
                    net,
                    name,
                )
            )
        for _ in range(3):
            route.vias.append(
                Via(
                    rng.uniform(5.0, 15.0),
                    rng.uniform(5.0, 15.0),
                    0.3,
                    rng.choice([0.5, 0.6, 0.8]),
                    (Layer.F_CU, Layer.B_CU),
                    net,
                    name,
                )
            )
        grid.mark_route(route)
    router = Router(grid, rules)
    router.set_net_name_to_id({name: net for net, name in NET_NAMES.items()})
    if pairwise:
        router.rules.pairwise_clearance = PairwiseClearanceTable(
            dru=0.15,
            net_voltages={"N1": 0, "N2": 300, "N3": 0, "N4": 120},
            required_by_pair={("N1", "N2"): 0.6, ("N3", "N4"): 0.4},
        )
    return grid, router


def _candidates(seed: int):
    """A mixed corpus of trace and via candidates across the populated area."""
    rng = random.Random(seed ^ 0x5240)
    out = []
    for _ in range(300):
        net = rng.choice(sorted(NET_NAMES))
        x, y = rng.uniform(4.0, 16.0), rng.uniform(4.0, 16.0)
        if rng.random() < 0.5:
            dx, dy = rng.choice([(0.2, 0.0), (0.0, 0.2), (0.15, 0.15), (0.15, -0.15)])
            out.append(
                Segment(
                    x,
                    y,
                    x + dx,
                    y + dy,
                    rng.choice([0.15, 0.2]),
                    rng.choice(LAYERS),
                    net,
                    NET_NAMES[net],
                )
            )
        else:
            out.append(
                Via(
                    x,
                    y,
                    0.3,
                    rng.choice([0.5, 0.6]),
                    rng.choice([(Layer.F_CU, Layer.B_CU), (Layer.F_CU, Layer.IN1_CU)]),
                    net,
                    NET_NAMES[net],
                )
            )
    return out


def _verdicts(grid, router, candidates, *, pruned: bool, monkeypatch):
    """Run the corpus through ``clear``; return verdicts + distance-call count."""
    monkeypatch.setattr(route_halo_geometry, "_PRUNE_BY_BOUNDS", pruned)
    calls = [0]
    original = BaseGeometry.distance

    def counting(self, other):
        calls[0] += 1
        return original(self, other)

    monkeypatch.setattr(BaseGeometry, "distance", counting)
    halo = grid._route_halo
    results = []
    for index, candidate in enumerate(candidates):
        kwargs: dict = {}
        if isinstance(candidate, Segment) and index % 3 == 0:
            partner = 2 if candidate.net != 2 else 3
            kwargs = {"partner_net": partner, "partner_clearance": 0.1}
        if index % 5 == 0:
            kwargs["require_geometry"] = False
        results.append(halo.clear(candidate, router, **kwargs))
    monkeypatch.undo()
    return results, calls[0]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("pairwise", [False, True])
def test_bounds_prune_matches_exhaustive_scan(seed, pairwise, monkeypatch):
    candidates = _candidates(seed)

    grid, router = _context(seed, pairwise=pairwise)
    reference, reference_calls = _verdicts(
        grid, router, candidates, pruned=False, monkeypatch=monkeypatch
    )

    grid, router = _context(seed, pairwise=pairwise)
    pruned, pruned_calls = _verdicts(grid, router, candidates, pruned=True, monkeypatch=monkeypatch)

    assert pruned == reference
    # Vacuity guard: the corpus must actually exercise both verdicts and the
    # prune must actually remove exact-distance work.
    assert any(reference) and not all(reference)
    assert pruned_calls < reference_calls


def test_bounds_prune_keeps_cross_bin_via_clearance(monkeypatch):
    """The #5240 prune must not undo the cross-bin lookup pinned by #5425."""
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    x, y = grid.grid_to_world(60, 60)
    route = Route(net=2, net_name="N2")
    route.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(route)
    router = Router(grid, rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    router.rules.via_clearance = 2.0
    segment = Segment(5.5, y, 5.5, y + 0.1, 0.2, Layer.F_CU, 1)
    assert not grid._route_halo.clear(segment, router)
    monkeypatch.setattr(route_halo_geometry, "_PRUNE_BY_BOUNDS", False)
    assert not grid._route_halo.clear(segment, router)
