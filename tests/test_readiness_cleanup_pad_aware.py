"""Readiness copper cleanup keeps mask-clean pad escapes (issue #9524).

The cleanup chain (``dedupe -> prune -> optimize-traces --drc-aware``) used to
undo a recipe's mask-clean pad escapes: it merged or re-bent copper across a
same-net pad centre and introduced bends inside a pad's mask-opening zone, and
each broken escape re-measures as a ``mask_to_copper`` finding.

The guard under test lives in
:mod:`kicad_tools.router.optimizer.geometry` (:class:`PadMaskZone`,
:func:`rewrite_keeps_pad_escapes`, :func:`snap_preserves_pad_zones`) and is
wired into ``kct optimize-traces`` (via ``TraceOptimizer.set_pad_zones``) and
``kct pcb prune`` (``plan_prune``).

The exposure count used here is the guard-zone measure -- how many
``(segment, pad opening)`` pairs carry copper that does not terminate on the
pad -- computed over exactly the zones the guard enforces.  It is the same
geometry ``mask_to_copper`` judges for the shapes these fixtures exercise;
the native checker's own count is what readiness gates in production.
"""

from __future__ import annotations

import math
from pathlib import Path

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.optimizer.geometry import (
    PadMaskZone,
    _layer_token,
    _terminates_on_pad,
    pad_mask_zones_from_pcb,
    rewrite_keeps_pad_escapes,
    snap_preserves_pad_zones,
)
from kicad_tools.router.optimizer.pcb import parse_segments
from kicad_tools.router.optimizer.trace import TraceOptimizer
from kicad_tools.router.primitives import Segment

W = 0.25
TOL = 1e-4


def _seg(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    net_name: str = "N1",
    net: int = 1,
    layer: object = Layer.F_CU,
    w: float = W,
) -> Segment:
    return Segment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=w,
        layer=layer,
        net=net,
        net_name=net_name,
    )


def _zone(
    x: float,
    y: float,
    half_w: float = 0.6,
    half_h: float = 0.45,
    margin: float = 0.0,
    net: str = "N1",
    layers: tuple[str, ...] = ("F.Cu",),
) -> PadMaskZone:
    return PadMaskZone(
        x=x,
        y=y,
        half_w=half_w,
        half_h=half_h,
        margin=margin,
        layers=frozenset(layers),
        net_name=net,
    )


# ---------------------------------------------------------------------------
# PadMaskZone geometry
# ---------------------------------------------------------------------------


class TestPadMaskZoneGeometry:
    def test_contains_point_respects_margin_and_copper(self):
        zone = _zone(0.0, 0.0, half_w=0.6, half_h=0.45, margin=0.05)
        assert zone.contains_point(0.6, 0.0, copper=True)
        assert zone.contains_point(0.63, 0.0)  # copper + margin
        assert not zone.contains_point(0.7, 0.0)
        assert not zone.contains_point(0.63, 0.0, copper=True)

    def test_segment_intersects_is_exact_against_the_box(self):
        zone = _zone(0.0, 0.0, half_w=0.6, half_h=0.45)
        # Straight through the middle.
        assert zone.segment_intersects(-2.0, 0.0, 2.0, 0.0)
        # Ends inside the box (a terminating entry).
        assert zone.segment_intersects(2.0, 2.0, 0.2, 0.2)
        # Passes just outside the top edge.
        assert not zone.segment_intersects(-2.0, 0.5, 2.0, 0.5)
        # Collinear with an edge line, outside the span.
        assert not zone.segment_intersects(1.0, 0.0, 2.0, 0.0)

    def test_covers_layer_and_through_hole_wildcard(self):
        smd = _zone(0.0, 0.0, layers=("F.Cu",))
        assert smd.covers_layer("F.Cu")
        assert not smd.covers_layer("B.Cu")
        tht = _zone(0.0, 0.0, layers=("*.Cu",))
        assert tht.covers_layer("B.Cu")


# ---------------------------------------------------------------------------
# Zone construction from a schema PCB
# ---------------------------------------------------------------------------


_ZONE_BOARD = """\
(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(generator_version "8.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(setup (pad_to_mask_clearance 0.02))
\t(net 0 "")
\t(net 1 "N1")
\t(net 2 "N2")
\t(gr_rect (start 90 30) (end 160 70)
\t\t(stroke (width 0.1) (type default))
\t\t(fill none)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge-rect")
\t)
\t(footprint "OVERRIDE_PAD"
\t\t(layer "F.Cu")
\t\t(uuid "fp-a")
\t\t(at 100 50)
\t\t(property "Reference" "A1" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-a1"))
\t\t(property "Value" "1k" (at 0 1.5) (layer "F.Fab") (uuid "val-a1"))
\t\t(pad "1" smd rect (at 0 0) (size 1.2 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(solder_mask_margin 0.1) (net 1 "N1"))
\t)
\t(footprint "FP_MARGIN"
\t\t(layer "F.Cu")
\t\t(uuid "fp-b")
\t\t(at 100 50 90)
\t\t(solder_mask_margin 0.05)
\t\t(property "Reference" "A2" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-a2"))
\t\t(property "Value" "1k" (at 0 1.5) (layer "F.Fab") (uuid "val-a2"))
\t\t(pad "1" smd rect (at 2 0) (size 0.8 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 1 "N1"))
\t)
\t(footprint "FP_BOARD"
\t\t(layer "F.Cu")
\t\t(uuid "fp-c")
\t\t(at 100 54)
\t\t(property "Reference" "A3" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-a3"))
\t\t(property "Value" "1k" (at 0 1.5) (layer "F.Fab") (uuid "val-a3"))
\t\t(pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6)
\t\t\t(layers "*.Cu" "*.Mask") (net 2 "N2"))
\t)
)
"""


def test_zones_from_pcb_position_layers_and_margin_resolution(tmp_path: Path):
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / "zones.kicad_pcb"
    path.write_text(_ZONE_BOARD)
    pcb = PCB.load(str(path))

    zones = pad_mask_zones_from_pcb(pcb)
    # Positions are board-relative (the schema space): sheet (100, 50) with
    # this board's origin (90, 30) is (10, 20) here.
    assert pcb._board_origin == (90.0, 30.0)
    by_net_and_pos = {(z.net_name, round(z.x, 3), round(z.y, 3)): z for z in zones}

    # Per-pad override wins; unrotated pad sits at the footprint position.
    override = by_net_and_pos[("N1", 10.0, 20.0)]
    assert override.margin == 0.1
    assert override.half_w == 0.6 and override.half_h == 0.45
    assert override.layers == frozenset({"F.Cu"})

    # Footprint override, footprint rotation applied to the pad offset:
    # (at 100 50 90) with pad local (2,0) lands at sheet (100, 98), i.e.
    # board-relative (10, 18).
    fp_margin = by_net_and_pos[("N1", 10.0, 18.0)]
    assert fp_margin.margin == 0.05

    # Board margin is the last fallback (FP_BOARD declares no override);
    # through-hole pads cover every layer. Sheet (100, 54) -> board (10, 24).
    tht = by_net_and_pos[("N2", 10.0, 24.0)]
    assert tht.margin == 0.02
    assert tht.layers == frozenset({"*.Cu"})
    assert tht.covers_layer("B.Cu")

    assert len(zones) == 3  # net-less pads would be skipped


# ---------------------------------------------------------------------------
# rewrite_keeps_pad_escapes: the optimize-traces chain guard
# ---------------------------------------------------------------------------


PAD = (100.0, 50.0)


class TestRewriteKeepsPadEscapes:
    def test_merge_across_same_net_pad_centre_is_vetoed(self):
        zones = [_zone(*PAD)]
        old = [_seg(99.0, 50.0, 100.0, 50.0), _seg(100.0, 50.0, 101.0, 50.0)]
        merged = [_seg(99.0, 50.0, 101.0, 50.0)]
        assert not rewrite_keeps_pad_escapes(old, merged, zones, TOL)

    def test_clean_pad_entry_unchanged_is_allowed(self):
        zones = [_zone(*PAD)]
        entry = [_seg(102.0, 52.0, 100.0, 50.0)]
        assert rewrite_keeps_pad_escapes(entry, entry, zones, TOL)

    def test_two_same_net_pads_joined_by_one_trace_may_merge(self):
        zones = [_zone(100.0, 50.0), _zone(108.0, 50.0)]
        old = [_seg(100.0, 50.0, 104.0, 50.0), _seg(104.0, 50.0, 108.0, 50.0)]
        merged = [_seg(100.0, 50.0, 108.0, 50.0)]
        assert rewrite_keeps_pad_escapes(old, merged, zones, TOL)

    def test_new_bend_inside_pad_zone_is_vetoed(self):
        zones = [_zone(*PAD)]
        old = [_seg(90.0, 50.0, 98.0, 50.0), _seg(102.0, 50.0, 110.0, 50.0)]
        # A rewrite that parks a kink inside the opening and crosses it.
        new = [
            _seg(90.0, 50.0, 100.3, 49.8),
            _seg(100.3, 49.8, 110.0, 50.0),
        ]
        assert not rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_foreign_pad_zone_crossing_is_vetoed(self):
        zones = [_zone(100.0, 50.0, net="N1"), _zone(104.0, 50.0, net="N2")]
        old = [_seg(90.0, 49.0, 92.0, 49.0)]
        new = [_seg(90.0, 49.0, 110.0, 50.0)]  # sweeps through the N2 opening
        assert not rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_other_layer_is_not_guarded_by_front_smd_zone(self):
        zones = [_zone(100.0, 50.0, layers=("F.Cu",))]
        old = [_seg(90.0, 50.0, 98.0, 50.0, layer=Layer.B_CU)]
        new = [_seg(90.0, 50.0, 110.0, 50.0, layer=Layer.B_CU)]
        assert rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_through_hole_zone_guards_back_layer(self):
        zones = [_zone(100.0, 50.0, layers=("*.Cu",))]
        old = [_seg(90.0, 50.0, 98.0, 50.0, layer=Layer.B_CU)]
        new = [_seg(90.0, 50.0, 110.0, 50.0, layer=Layer.B_CU)]
        assert not rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_same_net_crossing_without_terminating_is_vetoed(self):
        """A same-net crossing that keeps every endpoint outside the opening
        and misses the pad centre is still a ``same-net pass-through``
        exposure once its copper enters the opening: vetoed (the re-review
        of #9524 measured exactly this shape -- the optimizer's corner
        chamfer running across a sibling pad -- as a new exposure)."""
        zones = [_zone(100.0, 50.0)]
        old = [_seg(95.0, 56.5, 105.0, 46.5)]  # misses the opening entirely
        new = [_seg(95.0, 56.0, 105.0, 46.0)]  # clips the corner, misses centre
        assert not rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_same_net_straightened_across_pad_centre_is_vetoed(self):
        """The SMD shape of AC1: straightened copper across a same-net pad
        centre is the cleanup's own defect even with both ends far outside
        the opening (the through-hole test above is the ``*.Cu`` variant)."""
        zones = [_zone(100.0, 50.0)]
        old = [_seg(90.0, 50.0, 98.0, 50.0)]
        new = [_seg(90.0, 50.0, 110.0, 50.0)]
        assert not rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_via_in_pad_terminating_entry_is_allowed(self):
        zones = [_zone(*PAD)]
        old = [_seg(98.0, 52.0, 100.0, 50.0)]
        # Re-routed entry, still terminating on the pad centre (via-in-pad).
        new = [_seg(97.0, 53.0, 100.0, 50.0)]
        assert rewrite_keeps_pad_escapes(old, new, zones, TOL)

    def test_existing_copper_over_a_zone_is_exempt(self):
        zones = [_zone(*PAD)]
        crossing = [_seg(99.0, 50.0, 101.0, 50.0)]
        # old == new: the pass changed nothing, the board keeps what it had.
        assert rewrite_keeps_pad_escapes(crossing, crossing, zones, TOL)

    def test_empty_zones_and_trivial_rewrites_are_allowed(self):
        old = [_seg(0.0, 0.0, 1.0, 0.0)]
        assert rewrite_keeps_pad_escapes(old, old, [], TOL)
        zones = [_zone(50.0, 50.0)]
        assert rewrite_keeps_pad_escapes(old, old, zones, TOL)


# ---------------------------------------------------------------------------
# TraceOptimizer integration: the guard rides every pass
# ---------------------------------------------------------------------------


class _Clear:
    def path_is_clear(self, *args: object, **kwargs: object) -> bool:
        return True


class TestTraceOptimizerPadGuard:
    def _optimizer(self, zones: list[PadMaskZone]) -> TraceOptimizer:
        opt = TraceOptimizer(OptimizationConfig(tolerance=TOL), collision_checker=_Clear())
        opt.set_pad_zones(zones)
        return opt

    def test_merge_across_pad_centre_vetoed_far_merge_still_applied(self):
        zones = [_zone(*PAD)]
        opt = self._optimizer(zones)
        segments = [
            _seg(99.0, 50.0, 100.0, 50.0),
            _seg(100.0, 50.0, 101.0, 50.0),
            _seg(110.0, 60.0, 112.0, 60.0),
            _seg(112.0, 60.0, 114.0, 60.0),
        ]
        out = opt.optimize_segments(segments)
        by_ends = {((s.x1, s.y1), (s.x2, s.y2)) for s in out}
        # The escape across the pad centre is untouched...
        assert ((99.0, 50.0), (100.0, 50.0)) in by_ends
        assert ((100.0, 50.0), (101.0, 50.0)) in by_ends
        # ...while the far-away collinear pair away from pads still merges.
        assert ((110.0, 60.0), (114.0, 60.0)) in by_ends

    def test_without_zones_legacy_merge_behaviour_is_unchanged(self):
        opt = self._optimizer([])
        segments = [
            _seg(99.0, 50.0, 100.0, 50.0),
            _seg(100.0, 50.0, 101.0, 50.0),
        ]
        out = opt.optimize_segments(segments)
        assert len(out) == 1
        assert (out[0].x1, out[0].y1, out[0].x2, out[0].y2) == (99.0, 50.0, 101.0, 50.0)

    def test_pad_entry_straighten_is_still_allowed(self):
        # The #8325 geometry: a diagonal entry with a hair-step just before the
        # pad.  The straightened end sits within half a trace width of the
        # centre -- inside the pad copper -- so the escape stays clean.
        d = 1.62 / math.sqrt(2)
        pad = (2.0 + 0.005 + d, 2.0 + 0.127 + d)
        chain = [
            _seg(0.0, 0.0, 2.0, 2.0),
            _seg(2.0, 2.0, 2.0, 2.127),
            _seg(2.0, 2.127, 2.005, 2.127),
            _seg(2.005, 2.127, pad[0], pad[1]),
        ]
        zone = _zone(pad[0], pad[1], half_w=0.5, half_h=0.5)
        opt = TraceOptimizer(
            OptimizationConfig(
                tolerance=TOL,
                merge_collinear=False,
                eliminate_zigzags=False,
                compress_staircase=False,
                convert_45_corners=False,
                pull_tight=False,
                shorten_detours=False,
            ),
            collision_checker=_Clear(),
            net_anchors={1: [(0.0, 0.0), pad]},
        )
        opt.set_pad_points({"N1": [(0.0, 0.0), pad]})
        opt.set_pad_zones([zone])
        out = opt.optimize_segments(chain)
        assert len(out) < len(chain)
        assert rewrite_keeps_pad_escapes(chain, out, [zone], TOL)


# ---------------------------------------------------------------------------
# snap_preserves_pad_zones: the prune snap guard
# ---------------------------------------------------------------------------


class TestSnapPreservesPadZones:
    def test_snap_onto_target_pad_is_allowed(self):
        zones = [_zone(0.0, 0.0, net="N1"), _zone(5.0, 0.0, net="N2")]
        moves = [(0.4, 0.0, 3.0, 0.0, 0.0, 0.0, 3.0, 0.0, "F.Cu", "N1")]
        assert snap_preserves_pad_zones(zones, moves, (0.0, 0.0))

    def test_snap_swinging_through_foreign_zone_is_refused(self):
        zones = [_zone(0.0, 0.0, net="N1"), _zone(5.0, 0.0, net="N2")]
        moves = [(0.4, 0.0, 2.0, 0.0, 0.0, 0.0, 6.0, 0.0, "F.Cu", "N1")]
        assert not snap_preserves_pad_zones(zones, moves, (0.0, 0.0))

    def test_already_crossed_zone_is_exempt(self):
        zones = [_zone(5.0, 0.0, net="N2")]
        moves = [(0.0, 0.0, 6.0, 0.0, 0.0, 0.1, 6.0, 0.1, "F.Cu", "N1")]
        assert snap_preserves_pad_zones(zones, moves, (0.0, 0.0))

    def test_other_layer_moves_are_not_guarded(self):
        zones = [_zone(5.0, 0.0, net="N2", layers=("F.Cu",))]
        moves = [(0.4, 0.0, 2.0, 0.0, 0.0, 0.0, 6.0, 0.0, "B.Cu", "N1")]
        assert snap_preserves_pad_zones(zones, moves, (0.0, 0.0))


# ---------------------------------------------------------------------------
# Chain fixtures: written boards through the real tools
# ---------------------------------------------------------------------------


_BOARD = """\
(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(generator_version "8.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(setup (pad_to_mask_clearance 0))
\t(net 0 "")
\t(net 1 "N1")
\t(net 2 "N2")
\t(net 3 "N3")
\t(gr_rect (start 90 30) (end 160 80)
\t\t(stroke (width 0.1) (type default))
\t\t(fill none)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge-rect")
\t)
\t(footprint "PAD_N1"
\t\t(layer "F.Cu")
\t\t(uuid "fp-pad-n1")
\t\t(at 100 50)
\t\t(property "Reference" "A1" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-a1"))
\t\t(property "Value" "X" (at 0 1.5) (layer "F.Fab") (uuid "val-a1"))
\t\t(pad "1" smd rect (at 0 0) (size 1.2 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 1 "N1"))
\t)
\t(footprint "PAD_N2"
\t\t(layer "F.Cu")
\t\t(uuid "fp-pad-n2")
\t\t(at 130 50)
\t\t(property "Reference" "A2" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-a2"))
\t\t(property "Value" "X" (at 0 1.5) (layer "F.Fab") (uuid "val-a2"))
\t\t(pad "1" smd rect (at 0 0) (size 1.2 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 2 "N2"))
\t)
\t(footprint "PAD_N3"
\t\t(layer "F.Cu")
\t\t(uuid "fp-pad-n3")
\t\t(at 140 70)
\t\t(property "Reference" "A3" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-a3"))
\t\t(property "Value" "X" (at 0 1.5) (layer "F.Fab") (uuid "val-a3"))
\t\t(pad "1" smd rect (at 0 0) (size 1.2 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 3 "N3"))
\t)
\t(footprint "PAD_N3B"
\t\t(layer "F.Cu")
\t\t(uuid "fp-pad-n3b")
\t\t(at 150 70)
\t\t(property "Reference" "A4" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-a4"))
\t\t(property "Value" "X" (at 0 1.5) (layer "F.Fab") (uuid "val-a4"))
\t\t(pad "1" smd rect (at 0 0) (size 1.2 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 3 "N3"))
\t)
\t(segment (start {n1a}) (end {n1b}) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-n1-a"))
\t(segment (start {n1c}) (end {n1d}) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-n1-b"))
\t(segment (start 110 60) (end 112 60) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-n1-c"))
\t(segment (start 112 60) (end 114 60) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-n1-d"))
\t(segment (start 140.4 70) (end 149.6 70) (width 0.25) (layer "F.Cu") (net 3) (uuid "seg-n3-a"))
)
"""

# N1 escape collinear across the pad centre (entry + exit).
PASS_THROUGH_BOARD = _BOARD.format(n1a="99 50", n1b="100 50", n1c="100 50", n1d="101 50")
# Mask-clean escape: one straight 45-degree entry terminating on the centre,
# plus an inert segment away from every pad.
CLEAN_BOARD = _BOARD.format(n1a="102 52", n1b="100 50", n1c="120 55", n1d="124 55")

# A sibling same-net pad whose opening sits inside the sweep of the N1
# corner's 0.5 mm chamfer: the corner (100,50) chamfers to (99.5,50)--
# (100,50.5), crossing PAD_SIB's opening [99.35,99.75] x [50.1,50.5] while
# both chamfer endpoints stay outside it and the copper misses the pad
# centre by ~0.177 mm -- the re-review's measured ``same-net pass-through``
# shape.  The terminating entry keeps the pad a genuine sibling terminal.
CHAMFER_SWEEP_BOARD = """\
(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(generator_version "8.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(setup (pad_to_mask_clearance 0))
\t(net 0 "")
\t(net 1 "N1")
\t(gr_rect (start 90 30) (end 160 80)
\t\t(stroke (width 0.1) (type default))
\t\t(fill none) (layer "Edge.Cuts") (uuid "edge-rect"))
\t(footprint "PAD_SIB"
\t\t(layer "F.Cu") (uuid "fp-pad-sib")
\t\t(at 99.55 50.3)
\t\t(property "Reference" "S1" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-s1"))
\t\t(property "Value" "X" (at 0 1.5) (layer "F.Fab") (uuid "val-s1"))
\t\t(pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 1 "N1")))
\t(segment (start 94 50) (end 100 50) (width 0.25) (layer "F.Cu") (net 1)
\t\t(uuid "seg-n1-leg-a"))
\t(segment (start 100 50) (end 100 56) (width 0.25) (layer "F.Cu") (net 1)
\t\t(uuid "seg-n1-leg-b"))
\t(segment (start 99.55 52) (end 99.55 50.3) (width 0.25) (layer "F.Cu") (net 1)
\t\t(uuid "seg-n1-sib"))
)
"""


def _sheet_absolute_zones(pcb_path: Path) -> list[PadMaskZone]:
    """Zones in the segment-text coordinate space (schema + board origin)."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    origin_x, origin_y = getattr(pcb, "_board_origin", (0.0, 0.0))
    return [
        PadMaskZone(
            x=z.x + origin_x,
            y=z.y + origin_y,
            half_w=z.half_w,
            half_h=z.half_h,
            margin=z.margin,
            layers=z.layers,
            net_name=z.net_name,
        )
        for z in pad_mask_zones_from_pcb(pcb)
    ]


def _exposure_count(pcb_path: Path) -> int:
    """Guard-zone exposure count: ``(segment, opening)`` pairs whose copper
    enters a pad's mask-opening zone without terminating on the pad."""
    zones = _sheet_absolute_zones(pcb_path)
    segments = [seg for segs in parse_segments(pcb_path.read_text()).values() for seg in segs]
    count = 0
    for zone in zones:
        for seg in segments:
            if not zone.covers_layer(_layer_token(seg.layer)):
                continue
            if not zone.segment_intersects(seg.x1, seg.y1, seg.x2, seg.y2):
                continue
            if zone.net_name == seg.net_name and _terminates_on_pad(seg, zone, TOL):
                continue
            count += 1
    return count


class TestCleanupChainKeepsPadEscapes:
    def test_optimize_traces_keeps_escape_and_does_not_grow_exposure(self, tmp_path):
        """AC1/AC2/AC3: the pass-through merge is refused, the clean escape and
        the far-away merge behave, and the exposure count never grows."""
        from kicad_tools.cli import optimize_cmd

        for board_text in (CLEAN_BOARD, PASS_THROUGH_BOARD):
            src = tmp_path / "board.kicad_pcb"
            src.write_text(board_text)
            before = _exposure_count(src)

            out = tmp_path / "board-out.kicad_pcb"
            rc = optimize_cmd.main([str(src), "-o", str(out), "--format", "json", "-q"])
            assert rc == 0, board_text[:60]

            after = _exposure_count(out)
            assert after <= before

            segments = [seg for segs in parse_segments(out.read_text()).values() for seg in segs]
            by_ends = {((s.x1, s.y1), (s.x2, s.y2)) for s in segments}
            if board_text is CLEAN_BOARD:
                # The mask-clean escape is unchanged by the chain.
                assert ((102.0, 52.0), (100.0, 50.0)) in by_ends
            else:
                # No segment may now span across the pad centre: the entry
                # keeps its terminating endpoint at (100, 50).
                assert ((99.0, 50.0), (100.0, 50.0)) in by_ends
                assert ((100.0, 50.0), (101.0, 50.0)) in by_ends
                assert ((99.0, 50.0), (101.0, 50.0)) not in by_ends
            # Behaviour away from pads is unchanged: the far pair merges.
            assert ((110.0, 60.0), (114.0, 60.0)) in by_ends

    def test_optimize_traces_chamfer_sweep_over_sibling_pad_does_not_grow_exposure(self, tmp_path):
        """AC3 regression (the #9524 re-review's finding): a corner chamfer
        whose sweep crosses a *sibling same-net pad's* opening -- the shape
        the removed skirt exemption let through -- is refused, so the
        exposure count does not grow."""
        from kicad_tools.cli import optimize_cmd

        src = tmp_path / "chamfer-sweep.kicad_pcb"
        src.write_text(CHAMFER_SWEEP_BOARD)
        before = _exposure_count(src)
        assert before == 0

        out = tmp_path / "chamfer-sweep-out.kicad_pcb"
        rc = optimize_cmd.main([str(src), "-o", str(out), "--format", "json", "-q"])
        assert rc == 0

        assert _exposure_count(out) <= before

        segments = [seg for segs in parse_segments(out.read_text()).values() for seg in segs]
        by_ends = {((s.x1, s.y1), (s.x2, s.y2)) for s in segments}
        # The corner is not chamfered through the sibling pad's opening: the
        # L keeps its corner and no diagonal replaces it.
        assert ((94.0, 50.0), (100.0, 50.0)) in by_ends
        assert ((100.0, 50.0), (100.0, 56.0)) in by_ends
        assert all(abs(s.x1 - s.x2) < 0.01 or abs(s.y1 - s.y2) < 0.01 for s in segments)

    def test_prune_snaps_onto_pad_centre_and_leaves_escape_alone(self, tmp_path):
        """AC2 (prune side) + AC3: the N3 spur snaps onto its own pad centre
        (terminating entry), the N1 escape is untouched, exposure unchanged."""
        from kicad_tools.pcb_prune import apply_plans, plan_prune
        from kicad_tools.schema.pcb import PCB

        src = tmp_path / "board.kicad_pcb"
        src.write_text(PASS_THROUGH_BOARD)
        before = _exposure_count(src)

        pcb = PCB.load(str(src))
        plans = plan_prune(pcb)
        removed = {i for plan in plans for i in plan.remove}
        # The far-away collinear pair merges (second member removed); the
        # pass-through escape segments and the N3 spur are never removed.
        assert 3 in removed
        assert not removed & {0, 1, 4}

        snap_targets = []
        for plan in plans:
            for _idx, _which, point in plan.snaps:
                snap_targets.append(point)
        # The N3 spur's off-centre ends snap onto their own pad centres --
        # terminating entries, allowed by the guard's target exemption.
        # Plan coordinates are board-relative: sheet (140,70)/(150,70) with
        # this board's origin (90,30).
        assert (50.0, 40.0) in snap_targets
        assert (60.0, 40.0) in snap_targets

        stats = apply_plans(pcb, plans)
        assert stats["ends_snapped"] == 2
        out = tmp_path / "board-pruned.kicad_pcb"
        pcb.save(str(out))
        assert _exposure_count(out) <= before

        segments = [seg for segs in parse_segments(out.read_text()).values() for seg in segs]
        by_ends = {((s.x1, s.y1), (s.x2, s.y2)) for s in segments}
        assert ((140.0, 70.0), (150.0, 70.0)) in by_ends
        assert ((110.0, 60.0), (114.0, 60.0)) in by_ends
        assert ((99.0, 50.0), (100.0, 50.0)) in by_ends
        assert ((100.0, 50.0), (101.0, 50.0)) in by_ends
        assert ((99.0, 50.0), (101.0, 50.0)) not in by_ends


# ---------------------------------------------------------------------------
# Wiring: the prune snap guard must see (old, new) in the documented order
# ---------------------------------------------------------------------------


_SNAP_BOARD = """\
(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(generator_version "8.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(setup (pad_to_mask_clearance 0))
\t(net 0 "")
\t(net 1 "N1")
\t(net 2 "N2")
\t(gr_rect (start 90 30) (end 160 80)
\t\t(stroke (width 0.1) (type default))
\t\t(fill none) (layer "Edge.Cuts") (uuid "edge-rect"))
\t(footprint "P1"
\t\t(layer "F.Cu") (uuid "fp-p1") (at 100 50)
\t\t(property "Reference" "P1" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-p1"))
\t\t(property "Value" "X" (at 0 1.5) (layer "F.Fab") (uuid "val-p1"))
\t\t(pad "1" smd rect (at 0 0) (size 1.2 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 1 "N1")))
\t{p3}
\t(footprint "P2"
\t\t(layer "F.Cu") (uuid "fp-p2") (at {p2x} {p2y})
\t\t(property "Reference" "P2" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-p2"))
\t\t(property "Value" "X" (at 0 1.5) (layer "F.Fab") (uuid "val-p2"))
\t\t(pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 2 "N2")))
\t{extra}(segment (start {ax} {ay}) (end 112 48) (width 0.25) (layer "F.Cu") (net 1)
\t\t(uuid "seg-n1"))
)
"""

_P3_PAD = """\
\t(footprint "P3"
\t\t(layer "F.Cu") (uuid "fp-p3") (at 112 48)
\t\t(property "Reference" "P3" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-p3"))
\t\t(property "Value" "X" (at 0 1.5) (layer "F.Fab") (uuid "val-p3"))
\t\t(pad "1" smd rect (at 0 0) (size 1.2 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 1 "N1")))
"""

# For the reanchor shape: the moved segment's far end T-lands on this free
# track's centreline (a modelled join that is not a terminal), so P1 stays
# the net's only terminal and the re-anchor cannot change the partition.
_LAND_TRACK = """\
\t(segment (start 107 43) (end 117 53) (width 0.25) (layer "F.Cu") (net 1)
\t\t(uuid "seg-land"))
"""

# P2's opening [102.95, 103.45] x [49.15, 49.65] sits between the two aim
# lines onto P1's centre (100, 50) from (100.5, 50.3) / (100.7, 50.3): the
# re-aimed body crosses it, the pre-snap bodies pass clear above it.
_P2_IN_SWEEP = (103.2, 49.4)
_P2_CLEAR = (150.0, 70.0)


class TestPruneSnapGuardWiring:
    """Board-level regression tests for the ``_try_snap`` / ``_try_reanchor``
    call sites (issue #9524 review): a snap whose re-aimed body crosses a
    foreign pad's opening that the pre-snap body avoided must be refused --
    asserted through ``plan_prune`` itself, so an inverted ``(old, new)``
    argument order at a call site cannot pass unnoticed.  Each veto case has
    a control board (P2 out of the sweep) proving the same snap lands when
    no opening is crossed, so the refusal is the guard's, not some other
    precondition's."""

    def _run(self, tmp_path: Path, a: tuple[float, float], p2: tuple[float, float], *, reanchor):
        from kicad_tools.pcb_prune import apply_plans, plan_prune
        from kicad_tools.schema.pcb import PCB

        src = tmp_path / "board.kicad_pcb"
        src.write_text(
            _SNAP_BOARD.format(
                ax=a[0],
                ay=a[1],
                p2x=p2[0],
                p2y=p2[1],
                p3="" if reanchor else _P3_PAD,
                extra=_LAND_TRACK if reanchor else "",
            )
        )
        pcb = PCB.load(str(src))
        plans = plan_prune(pcb)
        targets = [point for plan in plans for _idx, _which, point in plan.snaps]
        stats = apply_plans(pcb, plans)
        out = tmp_path / "board-out.kicad_pcb"
        pcb.save(str(out))
        n1 = [s for s in parse_segments(out.read_text())["N1"] if (s.x1, s.y1) != (107.0, 43.0)]
        moved = [((s.x1, s.y1), (s.x2, s.y2)) for s in n1 if (s.x2, s.y2) == (112.0, 48.0)]
        return targets, stats, moved

    def test_snap_re_aim_through_foreign_opening_is_refused(self, tmp_path):
        # End anchored inside P1 at (100.5, 50.3); the snap onto P1's centre
        # (100, 50) re-aims the body down through P2's opening.
        targets, stats, moved = self._run(tmp_path, (100.5, 50.3), _P2_IN_SWEEP, reanchor=False)
        # Board-relative target of P1's centre: sheet (100, 50), origin (90, 30).
        assert (10.0, 20.0) not in targets
        assert stats["ends_snapped"] == 0
        assert moved == [((100.5, 50.3), (112.0, 48.0))]

    def test_snap_without_foreign_opening_lands(self, tmp_path):
        targets, stats, moved = self._run(tmp_path, (100.5, 50.3), _P2_CLEAR, reanchor=False)
        assert (10.0, 20.0) in targets
        assert stats["ends_snapped"] == 1
        assert moved == [((100.0, 50.0), (112.0, 48.0))]

    def test_reanchor_re_aim_through_foreign_opening_is_refused(self, tmp_path):
        # Overlap-held end outside P1 at (100.7, 50.3) (capsule contact only,
        # issue #8259); centring it on P1 re-aims the body through P2's opening.
        targets, stats, moved = self._run(tmp_path, (100.7, 50.3), _P2_IN_SWEEP, reanchor=True)
        assert (10.0, 20.0) not in targets
        assert stats["ends_snapped"] == 0
        assert moved == [((100.7, 50.3), (112.0, 48.0))]

    def test_reanchor_without_foreign_opening_lands(self, tmp_path):
        targets, stats, moved = self._run(tmp_path, (100.7, 50.3), _P2_CLEAR, reanchor=True)
        assert (10.0, 20.0) in targets
        assert stats["ends_snapped"] == 1
        assert moved == [((100.0, 50.0), (112.0, 48.0))]
