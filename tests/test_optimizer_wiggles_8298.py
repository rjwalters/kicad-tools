"""Issue #8298: wiggles that survived optimize-traces on a finished board."""

from __future__ import annotations

import math

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import OptimizationConfig
from kicad_tools.router.optimizer.algorithms import straighten_pad_entries_pass
from kicad_tools.router.optimizer.chain import sort_into_chains
from kicad_tools.router.optimizer.pcb import (
    _change_regions,
    _free_end_count,
    connectivity_preserved,
    salvage_connected_regions,
)
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


# -- 1. one unsafe edit no longer discards the net's safe edits ---------------


def test_change_regions_pair_a_replaced_chain_with_its_replacement():
    before = [_seg(0, 0, 1, 0), _seg(1, 0, 1, 0.2), _seg(1, 0.2, 3, 0.2)]
    after = [_seg(0, 0, 0.8, 0), _seg(0.8, 0, 1, 0.2), _seg(1, 0.2, 3, 0.2)]
    regions = _change_regions(before, after, 1e-4)
    assert len(regions) == 1
    removed, added = regions[0]
    assert len(removed) == 2 and len(added) == 2


def test_safe_region_is_kept_when_another_region_disconnects_a_pad():
    # Pads at both ends of a run on the left, and at both ends of a run on
    # the right.  The left rewrite straightens a jog (safe); the right one
    # deletes the run (splits its pads).
    left = [_seg(0, 0, 2, 0), _seg(2, 0, 2, 0.2), _seg(2, 0.2, 4, 0.2)]
    right = [_seg(10, 0, 14, 0)]
    before = left + right
    after = [_seg(0, 0, 1.8, 0), _seg(1.8, 0, 2, 0.2), _seg(2, 0.2, 4, 0.2)]
    anchors = [
        (0.0, 0.0, (Layer.F_CU,)),
        (4.0, 0.2, (Layer.F_CU,)),
        (10.0, 0.0, (Layer.F_CU,)),
        (14.0, 0.0, (Layer.F_CU,)),
    ]
    assert not connectivity_preserved(before, after, [], anchors, 1e-4)
    kept, n_kept, n_dropped = salvage_connected_regions(before, after, [], anchors, 1e-4)
    assert (n_kept, n_dropped) == (1, 1)
    assert sorted(_coords(kept)) == sorted(_coords(after + right))
    assert connectivity_preserved(before, kept, [], anchors, 1e-4)


def test_salvage_keeps_nothing_when_every_region_disconnects():
    before = [_seg(0, 0, 4, 0)]
    anchors = [(0.0, 0.0, (Layer.F_CU,)), (4.0, 0.0, (Layer.F_CU,))]
    kept, n_kept, n_dropped = salvage_connected_regions(before, [], [], anchors, 1e-4)
    assert (n_kept, n_dropped) == (0, 1)
    assert kept == before


# -- 2. a jog just before a pad is straightened into the pad -------------------


def test_pad_entry_jog_from_the_board_is_straightened():
    chain = _chain(J6_TAIL)
    out = straighten_pad_entries_pass(chain, OptimizationConfig(), _clear, [PAD])
    assert len(out) == 1
    seg = out[0]
    assert (seg.x1, seg.y1) == (152.446, 96.297)
    # The end lies on the first run's 45-degree line ...
    assert math.isclose(seg.x2 + seg.y2, 152.446 + 96.297, abs_tol=1e-6)
    # ... at the foot point of the pad centre, within half the trace width.
    offset = math.hypot(seg.x2 - PAD[0], seg.y2 - PAD[1])
    assert 0.08 < offset < 0.175
    # Shorter than the jogged tail.
    assert math.dist((seg.x1, seg.y1), (seg.x2, seg.y2)) < sum(
        math.dist((s.x1, s.y1), (s.x2, s.y2)) for s in chain
    )


def test_pad_entry_at_the_chain_start_is_straightened():
    flipped = [(x2, y2, x1, y1) for x1, y1, x2, y2 in reversed(J6_TAIL)]
    out = straighten_pad_entries_pass(_chain(flipped), OptimizationConfig(), _clear, [PAD])
    assert len(out) == 1
    assert (out[0].x2, out[0].y2) == (152.446, 96.297)


def test_pad_entry_offset_wider_than_half_the_trace_is_left():
    # The same geometry with a 0.1 mm trace: the 0.087 mm offset no longer
    # leaves the pad centre under the copper.
    chain = _chain(J6_TAIL, width=0.1)
    out = straighten_pad_entries_pass(chain, OptimizationConfig(), _clear, [PAD])
    assert _coords(out) == _coords(chain)


def test_pad_entry_needs_a_pad_a_clear_path_and_the_checker():
    chain = _chain(J6_TAIL)
    cfg = OptimizationConfig()
    assert _coords(straighten_pad_entries_pass(chain, cfg, _clear, [])) == _coords(chain)
    assert _coords(straighten_pad_entries_pass(chain, cfg, None, [PAD])) == _coords(chain)
    blocked = straighten_pad_entries_pass(chain, cfg, lambda s: False, [PAD])
    assert _coords(blocked) == _coords(chain)
    # A chain ending on a via (not in the pad list) is left alone.
    elsewhere = straighten_pad_entries_pass(chain, cfg, _clear, [(150.0, 90.0)])
    assert _coords(elsewhere) == _coords(chain)


def test_unchanged_chain_keeps_its_segment_objects():
    chain = _chain(J6_TAIL[:1])
    out = straighten_pad_entries_pass(chain, OptimizationConfig(), _clear, [PAD])
    assert out == chain and out[0] is chain[0]


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


def test_optimizer_straightens_the_pad_entry_on_both_layers():
    class _Clear:
        def path_is_clear(self, *a, **k):
            return True

    opt = TraceOptimizer(
        OptimizationConfig(),
        collision_checker=_Clear(),
        net_anchors={1: [PAD, (152.446, 96.297)]},
    )
    opt.set_pad_points({"N1": [(PAD[0], PAD[1], ())]})
    segs = _chain(J6_TAIL, layer=Layer.F_CU) + _chain(J6_TAIL, layer=Layer.B_CU)
    out = opt.optimize_segments(segs)
    by_layer = {layer: [s for s in out if s.layer == layer] for layer in (Layer.F_CU, Layer.B_CU)}
    assert all(len(v) == 1 for v in by_layer.values()), _coords(out)


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
