"""Issue #8298: wiggles that survived optimize-traces on a finished board."""

from __future__ import annotations

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import OptimizationConfig
from kicad_tools.router.optimizer.chain import sort_into_chains
from kicad_tools.router.optimizer.pcb import _free_end_count
from kicad_tools.router.optimizer.trace import TraceOptimizer
from kicad_tools.router.primitives import Segment

# The pad entry from the board: a 0.35 mm 45-degree run into a THT pad centre,
# sidestepping 0.087 mm through a 0.127 mm vertical and a 0.005 mm piece.
PAD = (158.103, 90.5176)
J6_TAIL = [
    (152.446, 96.297, 156.9545, 91.7885),
    (156.9545, 91.7885, 156.9545, 91.6615),
    (156.9545, 91.6615, 156.9591, 91.6615),
    (156.9591, 91.6615, 158.103, 90.5176),
]


def _seg(x1, y1, x2, y2, layer=Layer.F_CU, width=0.2, net=1):
    return Segment(
        x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer, net=net, net_name=f"N{net}"
    )


def _chain(points, layer=Layer.F_CU, width=0.35):
    return [_seg(*p, layer=layer, width=width) for p in points]


def _clear(_seg):
    return True


def _coords(segs):
    return [tuple(round(v, 4) for v in (s.x1, s.y1, s.x2, s.y2)) for s in segs]


def test_an_end_just_off_a_pad_centre_is_not_dangling():
    seg = _seg(0, 0, 5.08, 0.087, width=0.35)
    anchors = [(0.0, 0.0, ()), (5.0, 0.0, ())]
    assert _free_end_count([seg], [], anchors, 1e-4) == 0
    narrow = _seg(0, 0, 5.08, 0.087, width=0.1)
    assert _free_end_count([narrow], [], anchors, 1e-4) == 1


# -- chains on two layers that share their vertices ---------------------------


def test_routes_sharing_vertices_on_two_layers_stay_whole_chains():
    front = _chain(J6_TAIL, layer=Layer.F_CU)
    back = _chain(J6_TAIL, layer=Layer.B_CU)
    chains = sort_into_chains(front + back)
    assert sorted(len(c) for c in chains) == [4, 4]
    assert all(len({s.layer for s in c}) == 1 for c in chains)


def test_a_twin_route_on_another_layer_does_not_pin_a_jog():
    # The same 0.5 mm lateral jog on F.Cu and B.Cu.  Before #8298 each chain's
    # vertices counted as T-join anchors for the other layer's chain, so
    # neither jog could slide.
    class _Clear:
        def path_is_clear(self, *a, **k):
            return True

    jog = [(0, 0, 5, 0), (5, 0, 5, 0.5), (5, 0.5, 10, 0.5), (10, 0.5, 10, 5)]
    opt = TraceOptimizer(
        OptimizationConfig(
            eliminate_zigzags=False,
            compress_staircase=False,
            convert_45_corners=False,
            pull_tight=False,
            shorten_detours=False,
        ),
        collision_checker=_Clear(),
        net_anchors={1: [(0.0, 0.0), (10.0, 5.0)]},
    )
    segs = _chain(jog, layer=Layer.F_CU, width=0.2) + _chain(jog, layer=Layer.B_CU, width=0.2)
    out = opt.optimize_segments(segs)
    for layer in (Layer.F_CU, Layer.B_CU):
        assert sorted(_coords([s for s in out if s.layer == layer])) == [
            (0, 0, 10, 0),
            (10, 0, 10, 5),
        ], _coords(out)


def test_a_detour_ending_at_a_through_via_is_still_shortened():
    # A consumer board's AUDIO_R on B.Cu: a 45-degree "tent" between two vias,
    # with the route carrying on past the second via on the same layer.
    # Layer-aware chain sorting no longer cuts the route at that via (no other
    # layer's copper ends in the chain there), so the optimizer must cut it at
    # the via itself, or detour shortening sees one chain from the first via
    # to the far end and cannot replace the tent.
    class _Clear:
        def path_is_clear(self, *a, **k):
            return True

    v1, v2, far = (0.0, 0.0), (6.0, 0.0), (6.0, -5.0)
    tent = [(0, 0, 1.5, -1.5), (1.5, -1.5, 4.5, -1.5), (4.5, -1.5, 6, 0)]
    beyond = [(6, 0, 6, -5)]
    opt = TraceOptimizer(
        OptimizationConfig(),
        collision_checker=_Clear(),
        net_anchors={1: [v1, v2, far]},
    )
    out = opt.optimize_segments(_chain(tent + beyond, layer=Layer.B_CU, width=0.2))
    assert sorted(_coords(out)) == [(0, 0, 6, 0), (6, 0, 6, -5)], _coords(out)


def test_a_route_through_a_pad_centre_is_not_cut_there():
    # Only vias cut a chain: a route running through a pad centre on one layer
    # stays whole, as it did under layer-blind sorting.
    opt = TraceOptimizer(OptimizationConfig(), net_anchors={1: [(5.0, 0.0), (9.0, 0.0)]})
    opt.set_pad_points({"N1": [(5.0, 0.0)]})
    chain = _chain([(0, 0, 5, 0), (5, 0, 9, 0)], layer=Layer.F_CU, width=0.2)
    assert [len(p) for p in opt._split_at_anchors(chain)] == [2]
    opt.set_pad_points({})
    assert [len(p) for p in opt._split_at_anchors(chain)] == [1, 1]
