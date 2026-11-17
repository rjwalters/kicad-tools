"""Issue #6184: the rtree-less optimizer checker must see foreign copper.

``GridCollisionChecker`` is the trace optimizer's collision checker whenever
the optional ``rtree`` package is missing. Before #6184 it ran the exact
routed-copper kernel query only when its raster walk met a cell owned by a
foreign net. A raster cell has one owner, and the first marker wins. Where the
candidate's own clearance halo overlaps a neighbour's, the neighbour's copper
owns none of the cells the walk visits, so the checker accepted a path
0.05 mm from it. On board 03 that let ``compress_staircase`` move USB_D+ to
0.105 mm from USB_D-. The post-optimize backstop then demoted USB_D+
(13/13 -> 12/13), but only on installs without ``rtree``.

These tests pin the fix: the exact kernel query runs for every candidate the
raster did not refuse, through a broad-phase index that must agree with a
linear walk and must never be served stale.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router import DesignRules, Route, Segment
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.collision import (
    GridCollisionChecker,
    VectorCollisionChecker,
    _candidate_shape,
    _routed_copper_clear,
    _RoutedCopperIndex,
)
from kicad_tools.router.primitives import Via


def _seg(net: int, x1: float, y1: float, x2: float, y2: float, layer=Layer.F_CU) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=net, net_name=f"N{net}")


def _route(net: int, *segs: Segment, vias: tuple[Via, ...] = ()) -> Route:
    return Route(net=net, net_name=f"N{net}", segments=list(segs), vias=list(vias))


def _grid_with_neighbour(resolution: float, neighbour_y: float) -> RoutingGrid:
    """Net 1 marked first at y=2.5, then net 2 parallel at ``neighbour_y``.

    Marking net 1 first is what makes the overlapping halo cells net 1's, so
    net 2's copper is invisible to a raster walk along net 1.
    """
    rules = DesignRules(grid_resolution=resolution, trace_width=0.2, trace_clearance=0.15)
    grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
    grid.mark_route(_route(1, _seg(1, 1.0, 2.5, 5.0, 2.5)))
    grid.mark_route(_route(2, _seg(2, 1.0, neighbour_y, 5.0, neighbour_y)))
    return grid


class TestOwnershipHole:
    def test_refuses_candidate_hidden_behind_own_halo(self) -> None:
        """The exact pre-#6184 reproducer: 0.05 mm gap, accepted by the old walk."""
        grid = _grid_with_neighbour(0.25, 2.85)
        checker = GridCollisionChecker(grid)
        assert checker.path_is_clear(1.0, 2.6, 5.0, 2.6, Layer.F_CU, 0.2, exclude_net=1) is False

    def test_refuses_under_ignore_overflow_too(self) -> None:
        grid = _grid_with_neighbour(0.25, 2.85)
        grid._usage_count[:] = 2
        checker = GridCollisionChecker(grid, ignore_overflow=True)
        assert checker.path_is_clear(1.0, 2.6, 5.0, 2.6, Layer.F_CU, 0.2, exclude_net=1) is False

    @pytest.mark.parametrize("resolution", [0.25, 0.1, 0.05])
    @pytest.mark.parametrize(
        ("neighbour_y", "candidate_y", "legal"),
        [
            (2.85, 2.5, True),  # gap exactly 0.15 mm
            (2.85, 2.6, False),  # gap 0.05 mm
            (2.9, 2.5, True),
            (2.9, 2.6, False),  # gap 0.10 mm
            (3.0, 2.6, True),  # gap 0.20 mm
            (3.0, 2.7, False),
        ],
    )
    def test_verdict_matches_exact_gap(
        self, resolution: float, neighbour_y: float, candidate_y: float, legal: bool
    ) -> None:
        """No legal gap is newly refused, and no illegal one gets through."""
        grid = _grid_with_neighbour(resolution, neighbour_y)
        checker = GridCollisionChecker(grid)
        verdict = checker.path_is_clear(
            1.0, candidate_y, 5.0, candidate_y, Layer.F_CU, 0.2, exclude_net=1
        )
        assert verdict is legal

    @pytest.mark.parametrize("candidate_y", [2.5, 2.6, 2.7])
    def test_agrees_with_vector_checker(self, candidate_y: float) -> None:
        """The two ``CollisionChecker`` implementations answer the same."""
        grid = _grid_with_neighbour(0.25, 2.85)
        if not grid._rtree_available:
            pytest.skip("rtree not installed; VectorCollisionChecker unavailable")
        args = (1.0, candidate_y, 5.0, candidate_y, Layer.F_CU, 0.2)
        assert GridCollisionChecker(grid).path_is_clear(
            *args, exclude_net=1
        ) == VectorCollisionChecker(grid).path_is_clear(*args, exclude_net=1)


class TestRoutedCopperIndex:
    def test_rebuilt_after_new_copper(self) -> None:
        """A cached index is never served stale after the copper changes."""
        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
        grid.mark_route(_route(1, _seg(1, 1.0, 2.0, 5.0, 2.0)))
        checker = GridCollisionChecker(grid)
        args = (1.0, 2.0, 5.0, 2.0, Layer.F_CU, 0.2)
        assert checker.path_is_clear(*args, exclude_net=1) is True

        # A foreign crossing lands after the index was first built.
        grid.mark_route(_route(2, _seg(2, 3.0, 1.0, 3.0, 3.0)))
        assert checker.path_is_clear(*args, exclude_net=1) is False

        grid.unmark_route(grid.routes[-1])
        assert checker.path_is_clear(*args, exclude_net=1) is True

    def test_index_matches_linear_walk(self) -> None:
        """Seeded property test: the binned broad phase loses nothing."""
        rng = random.Random(6184)
        rules = DesignRules(
            grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15, via_clearance=0.2
        )
        grid = RoutingGrid(width=20.0, height=20.0, rules=rules)
        layers = (Layer.F_CU, Layer.B_CU)
        for net in range(2, 30):
            x, y = rng.uniform(1, 19), rng.uniform(1, 19)
            segs = []
            for _ in range(rng.randint(1, 4)):
                nx = min(19.0, max(1.0, x + rng.uniform(-4, 4)))
                ny = min(19.0, max(1.0, y + rng.uniform(-4, 4)))
                segs.append(_seg(net, x, y, nx, ny, layer=rng.choice(layers)))
                x, y = nx, ny
            vias = ()
            if rng.random() < 0.4:
                vias = (
                    Via(
                        x=x,
                        y=y,
                        drill=0.3,
                        diameter=0.6,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=net,
                        net_name=f"N{net}",
                    ),
                )
            grid.routes.append(_route(net, *segs, vias=vias))
        index = _RoutedCopperIndex(grid.routes)

        refused = 0
        for _ in range(400):
            x1, y1 = rng.uniform(0, 20), rng.uniform(0, 20)
            x2, y2 = x1 + rng.uniform(-3, 3), y1 + rng.uniform(-3, 3)
            layer = rng.choice(layers)
            exclude = rng.randint(1, 30)
            candidate = _candidate_shape(x1, y1, x2, y2, 0.2)
            layer_idx = grid.layer_to_index(layer.value)
            linear = _routed_copper_clear(grid, candidate, layer, layer_idx, exclude)
            binned = _routed_copper_clear(grid, candidate, layer, layer_idx, exclude, index=index)
            assert binned == linear, (x1, y1, x2, y2, layer, exclude)
            refused += not linear
        # The corpus must exercise both verdicts to mean anything.
        assert 0 < refused < 400
