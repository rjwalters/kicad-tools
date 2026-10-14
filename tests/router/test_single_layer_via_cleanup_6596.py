"""Single-layer junction vias are dropped by ``cleanup_artifacts`` (Issue #6596).

Board 01's VOUT net reached ``kct route``'s output with a through via at an
RSMT Steiner branch point whose three segments all ran on B.Cu: KiCad
reported it as ``via_dangling``.  ``Autorouter.cleanup_artifacts`` now drops
such a via -- but only when the layer-aware copper of its net stays exactly
as connected without it.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.terminal_connectivity import (
    copper_component_count,
    single_layer_via_indices,
)

NET = 2
NET_NAME = "VOUT"

# Board-01-shaped geometry: three SMD pads on F.Cu and a Steiner tee at TEE.
R1 = (110.0, 90.0)
R2 = (110.0, 100.0)
J2 = (120.0, 95.0)
TEE = (112.0, 95.0)


def _seg(x1, y1, x2, y2, layer, net=NET) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.5, layer=layer, net=net, net_name=NET_NAME)


def _via(x, y, net=NET, **kwargs) -> Via:
    return Via(
        x=x,
        y=y,
        drill=0.3,
        diameter=0.8,
        layers=(Layer.F_CU, Layer.B_CU),
        net=net,
        net_name=NET_NAME,
        **kwargs,
    )


def _router(with_pads: bool = True) -> Autorouter:
    router = Autorouter(width=50.0, height=40.0, origin_x=100.0, origin_y=80.0)
    if with_pads:
        for ref, (x, y) in (("R1", R1), ("R2", R2), ("J2", J2)):
            router.add_component(
                ref,
                [
                    {
                        "number": "1",
                        "x": x,
                        "y": y,
                        "width": 1.0,
                        "height": 1.0,
                        "net": NET,
                        "net_name": NET_NAME,
                        "layer": Layer.F_CU,
                    }
                ],
            )
    return router


def _tee_routes(tee_via: Via | None = None) -> list[Route]:
    """R1 -> tee (ends with a via onto F.Cu), tee -> R2, tee -> J2 on B.Cu."""
    tee_via = tee_via if tee_via is not None else _via(*TEE)
    route0 = Route(
        net=NET,
        net_name=NET_NAME,
        segments=[
            _seg(*R1, 110.0, 93.0, Layer.F_CU),
            _seg(110.0, 93.0, *TEE, Layer.B_CU),
        ],
        vias=[_via(110.0, 93.0), tee_via],
    )
    route1 = Route(
        net=NET,
        net_name=NET_NAME,
        segments=[
            _seg(*TEE, 112.0, 97.0, Layer.B_CU),
            _seg(112.0, 97.0, *R2, Layer.F_CU),
        ],
        vias=[_via(112.0, 97.0)],
    )
    route2 = Route(
        net=NET,
        net_name=NET_NAME,
        segments=[
            _seg(*TEE, 114.0, 97.0, Layer.B_CU),
            _seg(114.0, 97.0, *J2, Layer.F_CU),
        ],
        vias=[_via(114.0, 97.0)],
    )
    return [route0, route1, route2]


def _net_components(router: Autorouter) -> int:
    pads = [router.pads[k] for k in router.nets[NET]]
    segments = [s for r in router.routes for s in r.segments]
    vias = [v for r in router.routes for v in r.vias]
    return copper_component_count(pads, segments, vias)


def _via_positions(router: Autorouter) -> set[tuple[float, float]]:
    return {(v.x, v.y) for r in router.routes for v in r.vias}


class TestSingleLayerTeeVia:
    def test_tee_via_removed_and_net_stays_connected(self):
        router = _router()
        router.routes = _tee_routes()
        segments_before = [s for r in router.routes for s in r.segments]
        assert _net_components(router) == 1

        stats = router.cleanup_artifacts()

        assert stats["single_layer_vias_removed"] == 1
        assert TEE not in _via_positions(router)
        # The three real layer transitions are untouched.
        assert _via_positions(router) == {(110.0, 93.0), (112.0, 97.0), (114.0, 97.0)}
        # The B.Cu tee segments are all kept.
        assert [s for r in router.routes for s in r.segments] == segments_before
        assert _net_components(router) == 1

    def test_two_layer_via_is_untouched(self):
        """Control: every via that bonds F.Cu and B.Cu survives."""
        router = _router()
        routes = _tee_routes()
        # Replace the tee's B.Cu stub to R2 with an F.Cu one: now the tee
        # via bonds both layers and is a real transition.
        routes[1].segments[0] = _seg(*TEE, 112.0, 97.0, Layer.F_CU)
        routes[1].vias = []
        routes[1].segments[1] = _seg(112.0, 97.0, *R2, Layer.F_CU)
        router.routes = routes

        stats = router.cleanup_artifacts()

        assert stats["single_layer_vias_removed"] == 0
        assert TEE in _via_positions(router)

    def test_via_bridging_disjoint_same_layer_copper_is_kept(self):
        """Two B.Cu stubs that meet only through the via pad keep the via."""
        router = _router()
        routes = _tee_routes()
        tx, ty = TEE
        # Pull both branch stubs back so they no longer touch route 0's
        # segment end or each other -- only the via pad joins them.
        routes[0].segments[1] = _seg(110.0, 93.0, tx - 0.35, ty, Layer.B_CU)
        routes[1].segments[0] = _seg(tx + 0.35, ty, 112.0, 97.0, Layer.B_CU)
        routes[2].segments[0] = _seg(tx + 0.35, ty, 114.0, 97.0, Layer.B_CU)
        router.routes = routes
        assert _net_components(router) == 1

        stats = router.cleanup_artifacts()

        assert stats["single_layer_vias_removed"] == 0
        assert TEE in _via_positions(router)
        assert _net_components(router) == 1

    def test_via_over_same_net_zone_is_kept(self):
        """A via bonded through same-net zone fill on F.Cu is a stitching via."""
        router = _router()
        router.routes = _tee_routes()
        grid = router.grid
        if not hasattr(grid, "_is_zone"):
            pytest.skip("grid backend exposes no zone arrays")
        gx, gy = grid.world_to_grid(*TEE)
        f_idx = grid.layer_to_index(Layer.F_CU.value)
        grid._is_zone[f_idx, gy, gx] = True
        grid._net[f_idx, gy, gx] = NET

        stats = router.cleanup_artifacts()

        assert stats["single_layer_vias_removed"] == 0
        assert TEE in _via_positions(router)

    def test_preserved_copper_bonds_the_via(self):
        """Pre-existing F.Cu copper at the via counts as its second layer."""
        router = _router()
        router.routes = _tee_routes()
        router.existing_routes = [
            Route(
                net=NET,
                net_name=NET_NAME,
                segments=[_seg(*TEE, 112.0, 92.0, Layer.F_CU)],
            )
        ]

        stats = router.cleanup_artifacts()

        assert stats["single_layer_vias_removed"] == 0
        assert TEE in _via_positions(router)

    @pytest.mark.parametrize("flag", ["is_micro", "in_pad"])
    def test_micro_and_in_pad_vias_are_kept(self, flag):
        router = _router()
        router.routes = _tee_routes(tee_via=_via(*TEE, **{flag: True}))

        stats = router.cleanup_artifacts()

        assert stats["single_layer_vias_removed"] == 0
        assert TEE in _via_positions(router)

    def test_via_touching_a_pad_is_kept(self):
        """A via on a same-net pad is never a candidate, whatever its layers."""
        router = _router()
        pad = router.pads[router.nets[NET][0]]
        via = _via(pad.x, pad.y)
        router.routes = [
            Route(
                net=NET,
                net_name=NET_NAME,
                segments=[_seg(pad.x, pad.y, pad.x, pad.y + 3.0, Layer.F_CU)],
                vias=[via],
            )
        ]

        stats = router.cleanup_artifacts()

        assert stats["single_layer_vias_removed"] == 0
        assert router.routes[0].vias == [via]


class TestSingleLayerViaIndices:
    def test_isolated_via_is_a_candidate(self):
        assert single_layer_via_indices([], [], [_via(*TEE)]) == [0]

    def test_overlapping_vias_bond_each_other(self):
        """Two stacked vias both span F.Cu and B.Cu, so neither is nominated."""
        assert single_layer_via_indices([], [], [_via(*TEE), _via(*TEE)]) == []
