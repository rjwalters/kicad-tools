"""Issue #8250: optimize-traces must not break attachments, holes or connectivity."""

from __future__ import annotations

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import OptimizationConfig
from kicad_tools.router.optimizer.algorithms import convert_corners_45
from kicad_tools.router.optimizer.collision import HoleClearanceChecker
from kicad_tools.router.optimizer.pcb import (
    _PAD_VIOLATION_CATEGORIES,
    _STALE_FILL_RULES,
    connectivity_preserved,
)
from kicad_tools.router.optimizer.trace import TraceOptimizer
from kicad_tools.router.primitives import Segment, Via


def _seg(x1, y1, x2, y2, layer=Layer.F_CU):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=1, net_name="N")


def _corner():
    return [_seg(0, 0, 5, 0), _seg(5, 0, 5, 5)]


def test_unprotected_corner_is_chamfered():
    out = convert_corners_45(_corner(), OptimizationConfig())
    assert len(out) == 3


def test_via_on_corner_left_unchanged():
    segs = _corner()
    out = convert_corners_45(segs, OptimizationConfig(), protected_vertices=[(5.0, 0.0)])
    assert out == segs


def test_t_junction_on_corner_left_unchanged():
    segs = _corner() + [_seg(5, 0, 8, 0)]  # third segment at the corner vertex
    opt = TraceOptimizer(
        OptimizationConfig(
            merge_collinear=False,
            eliminate_zigzags=False,
            compress_staircase=False,
            pull_tight=False,
            shorten_detours=False,
        )
    )
    out = opt.optimize_segments(segs)
    assert {(s.x1, s.y1, s.x2, s.y2) for s in out} == {(s.x1, s.y1, s.x2, s.y2) for s in segs}


def test_attachment_point_protects_corner():
    opt = TraceOptimizer(
        OptimizationConfig(
            merge_collinear=False,
            eliminate_zigzags=False,
            compress_staircase=False,
            pull_tight=False,
            shorten_detours=False,
        )
    )
    opt.set_attachment_points({"N": [(5.0, 0.0)]})
    segs = _corner()
    assert opt.optimize_segments(segs) == segs


class _AlwaysClear:
    def path_is_clear(self, *a, **k):
        return True


def test_hole_blocks_candidate_path():
    chk = HoleClearanceChecker(_AlwaysClear(), [(5.0, 1.0, 1.0)], 0.25)
    assert not chk.path_is_clear(0, 0, 10, 0, Layer.F_CU, 0.2, 1)
    assert chk.path_is_clear(0, -5, 10, -5, Layer.F_CU, 0.2, 1)


def test_disconnecting_rewrite_detected():
    before = [_seg(0, 0, 5, 0), _seg(5, 0, 10, 0)]
    anchors = [(0.0, 0.0, (Layer.F_CU,)), (10.0, 0.0, (Layer.F_CU,))]
    assert connectivity_preserved(before, [_seg(0, 0, 10, 0)], [], anchors, 0.01)
    # rewrite leaves a gap between the two ends
    assert not connectivity_preserved(
        before, [_seg(0, 0, 4, 0), _seg(6, 0, 10, 0)], [], anchors, 0.01
    )


def test_via_joins_layers_for_connectivity():
    b = Layer.B_CU
    before = [_seg(0, 0, 5, 0), _seg(5, 0, 10, 0, b)]
    via = Via(x=5.0, y=0.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, b), net=1, net_name="N")
    anchors = [(0.0, 0.0, (Layer.F_CU,)), (10.0, 0.0, (b,))]
    assert connectivity_preserved(before, before, [via], anchors, 0.01)
    moved = [_seg(0, 0, 4, 0), _seg(5, 0, 10, 0, b)]  # F.Cu no longer reaches the via
    assert not connectivity_preserved(before, moved, [via], anchors, 0.01)


def test_stale_zone_fill_not_a_rollback_category():
    assert "clearance_segment_zone" in _STALE_FILL_RULES
    assert "clearance_segment_zone" not in _PAD_VIOLATION_CATEGORIES
