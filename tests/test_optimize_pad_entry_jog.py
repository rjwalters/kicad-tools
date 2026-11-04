"""Pad-entry jog straightening and the matching connectivity-gate radius (Issue #8325)."""

from __future__ import annotations

import math

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.algorithms import straighten_pad_entry_pass
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.optimizer.pcb import _free_end_count, connectivity_preserved
from kicad_tools.router.optimizer.trace import TraceOptimizer
from kicad_tools.router.primitives import Segment, Via

W = 0.35
TOL = 1e-4


def _seg(x1, y1, x2, y2, w=W):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=w, layer=Layer.F_CU, net=1, net_name="N")


def _pad_entry_chain():
    """Diagonal, 0.127 mm vertical + 0.005 mm horizontal step, 1.62 mm diagonal to a pad."""
    d = 1.62 / math.sqrt(2)
    p0 = (0.0, 0.0)
    p1 = (2.0, 2.0)
    p2 = (p1[0], p1[1] + 0.127)
    p3 = (p2[0] + 0.005, p2[1])
    p4 = (p3[0] + d, p3[1] + d)
    chain = [_seg(*p0, *p1), _seg(*p1, *p2), _seg(*p2, *p3), _seg(*p3, *p4)]
    return chain, p4


def _cfg():
    return OptimizationConfig(tolerance=TOL)


def test_gate_counts_end_within_half_width_as_touching_pad():
    anchors = [(1.0 + 0.087, 1.0, (Layer.F_CU,))]
    seg = [_seg(0.0, 1.0, 1.0, 1.0)]
    # One end sits 0.087 mm from the pad centre (< w/2): only the free far end counts.
    assert _free_end_count(seg, [], anchors, TOL) == 1


def test_gate_end_outside_half_width_is_free():
    anchors = [(1.0 + 0.2, 1.0, (Layer.F_CU,))]
    assert _free_end_count([_seg(0.0, 1.0, 1.0, 1.0)], [], anchors, TOL) == 2


def test_gate_end_on_other_layer_is_free():
    anchors = [(1.0, 1.0, (Layer.B_CU,))]
    assert _free_end_count([_seg(0.0, 1.0, 1.0, 1.0)], [], anchors, TOL) == 2


def test_gate_via_end_needs_tolerance():
    via = Via(x=1.087, y=1.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
    assert _free_end_count([_seg(0.0, 1.0, 1.0, 1.0)], [via], [], TOL) == 2


def test_pad_entry_geometry_is_straightened_and_accepted_by_gate():
    chain, pad = _pad_entry_chain()
    anchors = [(0.0, 0.0, ()), (pad[0], pad[1], ())]
    out = straighten_pad_entry_pass(chain, _cfg(), lambda s: True, [(0, 0), pad], [(0, 0), pad])
    assert len(out) == 1
    assert connectivity_preserved(chain, out, [], anchors, TOL)
    # The old gate (bare tolerance) would have rejected this end.
    end = (out[0].x2, out[0].y2)
    assert 1e-3 < math.hypot(end[0] - pad[0], end[1] - pad[1]) <= W / 2


def test_optimizer_straightens_pad_entry():
    chain, pad = _pad_entry_chain()
    opt = TraceOptimizer(_cfg(), collision_checker=_Clear(), net_anchors={1: [(0.0, 0.0), pad]})
    opt.set_pad_points({"N": [(0.0, 0.0), pad]})
    out = opt.optimize_segments(chain)
    assert len(out) < len(chain)


class _Clear:
    def path_is_clear(self, *a, **k):
        return True


def test_via_end_is_not_straightened():
    chain, pad = _pad_entry_chain()
    out = straighten_pad_entry_pass(chain, _cfg(), lambda s: True, [(0, 0), pad], [(9.0, 9.0)])
    assert out == chain


def test_end_off_half_width_not_straightened():
    chain, pad = _pad_entry_chain()
    narrow = [_seg(s.x1, s.y1, s.x2, s.y2, w=0.1) for s in chain]
    out = straighten_pad_entry_pass(narrow, _cfg(), lambda s: True, [(0, 0), pad], [(0, 0), pad])
    assert out == narrow  # 0.087 mm off > w/2 = 0.05


def test_blocked_path_is_kept():
    chain, pad = _pad_entry_chain()
    out = straighten_pad_entry_pass(chain, _cfg(), lambda s: False, [(0, 0), pad], [(0, 0), pad])
    assert out == chain


def test_noop_without_pad_points_or_anchors():
    chain, pad = _pad_entry_chain()
    assert straighten_pad_entry_pass(chain, _cfg(), lambda s: True, [pad], None) == chain
    assert straighten_pad_entry_pass(chain, _cfg(), lambda s: True, [pad], []) == chain


def test_anchor_only_the_dropped_copper_covers_blocks_rewrite():
    chain, pad = _pad_entry_chain()
    last = chain[-1]
    mx, my = (last.x1 + last.x2) / 2, (last.y1 + last.y2) / 2
    k = 0.15 / math.sqrt(2)
    tap = (mx - k, my + k)  # on the far side of the 1.62 mm run from the new line
    out = straighten_pad_entry_pass(chain, _cfg(), lambda s: True, [(0, 0), pad, tap], [pad])
    assert out == chain
