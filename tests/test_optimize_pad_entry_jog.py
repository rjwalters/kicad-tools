"""Pad-entry jog straightening and the matching connectivity-gate radius (Issue #8325).

Also the pad-escape mask guard cases raised by issue #9748: an on-axis pad
entry may not be re-angled into an off-axis one, and same-net copper may not
be slid into the mask-clearance band around another pad's opening.
"""

from __future__ import annotations

import math

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.algorithms import straighten_pad_entry_pass
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.optimizer.geometry import PadMaskZone, rewrite_keeps_pad_escapes
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


# ---------------------------------------------------------------------------
# Issue #9748: the mask guard must keep pad escapes mask-clean
# ---------------------------------------------------------------------------


def _zone_9748(**kw):
    """A flat SMD pad zone at (100, 50): 1.2 x 0.6 mm copper, no margin."""
    kw.setdefault("x", 100.0)
    kw.setdefault("y", 50.0)
    kw.setdefault("half_w", 0.6)
    kw.setdefault("half_h", 0.3)
    kw.setdefault("margin", 0.0)
    kw.setdefault("net_name", "N")
    kw.setdefault("layers", ("F.Cu",))
    return PadMaskZone(layers=frozenset(kw.pop("layers")), **kw)


class TestMaskEscapeGuard9748:
    def test_on_axis_entry_rewrite_is_kept(self):
        """AC: a straight on-axis entry stays; only its tail is trimmed."""
        zones = [_zone_9748()]
        old = [_seg(97.0, 50.0, 100.2, 50.0)]
        new = [_seg(97.5, 50.0, 100.2, 50.0)]
        assert rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_on_axis_entry_is_not_re_angled_off_axis(self):
        """The #9748 leak: a re-angle that still ends in the pad copper and
        runs past its centre, but crosses the opening edge at ~31 degrees --
        the shallow crossing leaves copper beside the pad that the one clean
        entry allowance does not cover (a ``same-net own-pad entry`` error).
        The pre-#9748 guard (terminating end + centre proximity) let it by."""
        zones = [_zone_9748()]
        on_axis = [_seg(97.0, 50.0, 100.2, 50.0)]
        # Line through the pad centre at slope 0.6 (atan(0.6) ~ 31 degrees),
        # ending inside the copper past the centre.
        off_axis = [_seg(99.1, 49.46, 100.3, 50.18)]
        assert rewrite_keeps_pad_escapes(on_axis, on_axis, zones, TOL)
        assert not rewrite_keeps_pad_escapes(on_axis, off_axis, zones, TOL)

    def test_clean_45_degree_entry_is_still_allowed(self):
        """45 degrees is the one-clean-entry boundary, not a veto."""
        zones = [_zone_9748()]
        old = [_seg(97.0, 52.05, 100.0, 50.0)]
        new = [_seg(98.0, 52.05, 100.05, 50.0)]
        assert rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_same_net_track_not_moved_into_mask_clearance_band(self):
        """The #9748 disjoint class: a same-net track slid to ~0.05 mm from a
        neighbouring same-net pad's opening never touches the opening, yet
        sits inside the fab's mask-to-copper clearance band.  With the band
        engaged (the jlcpcb policy floor) the move is vetoed; without a
        policy the pre-#9748 opening-only guard applies."""
        zones = [_zone_9748(x=110.0, half_w=0.6, half_h=0.6, margin=0.05)]
        old = [
            _seg(100.0, 50.8, 106.0, 50.8, w=0.25),
            _seg(106.0, 50.8, 112.0, 50.8, w=0.25),
        ]
        merged = [_seg(100.0, 50.8, 112.0, 50.8, w=0.25)]
        # Opening spans y in [49.35, 50.65]; the merged capsule's near edge
        # reaches y = 50.675: outside the opening, 0.025 mm inside the band.
        assert rewrite_keeps_pad_escapes(old, merged, zones, TOL)
        assert not rewrite_keeps_pad_escapes(old, merged, zones, TOL, 0.09)

    def test_band_veto_spares_a_terminating_entry_on_that_pad(self):
        """A same-net entry onto the band's own pad keeps its exemption: the
        steep crossing's band copper stays inside the one-entry allowance."""
        zones = [_zone_9748(x=110.0, half_w=0.6, half_h=0.6, margin=0.05)]
        old = [_seg(108.0, 52.0, 110.0, 50.0)]
        new = [_seg(108.5, 51.5, 110.0, 50.0)]
        assert rewrite_keeps_pad_escapes(old, new, zones, TOL, 0.09)

    def test_optimizer_passes_respect_the_band(self):
        """The TraceOptimizer wiring: collinear copper running past a pad's
        opening band is not merged into one segment when the jlcpcb
        clearance is registered (Issue #9748's optimize-traces path)."""
        zone = _zone_9748(x=110.0, half_w=0.6, half_h=0.6, margin=0.05, net_name="N")
        chain = [
            _seg(100.0, 50.8, 106.0, 50.8, w=0.25),
            _seg(106.0, 50.8, 112.0, 50.8, w=0.25),
        ]
        opt_plain = TraceOptimizer(_cfg(), collision_checker=_Clear())
        assert len(opt_plain.optimize_segments(list(chain))) == 1
        opt_band = TraceOptimizer(_cfg(), collision_checker=_Clear())
        opt_band.set_pad_zones([zone], danger_clearance=0.09)
        assert len(opt_band.optimize_segments(list(chain))) == 2
