"""A completion link starts from kept copper already on its pad (issue #9636).

``kct route --complete`` keeps the board's copper and asks the lattice engine
only for the missing joins.  A join that leaves a pin which ALREADY carries a
(narrower than class width) escape track used to re-escape the pin from its
pad centre at the net's class width and decline ``pad-escape-start`` -- even
though a track demonstrably leaves it.

Layout used here (mm): net-1 pin ``U1.1`` sits in a corridor between two
long other-net pads.  The corridor leaves 0.30 mm from the pin centreline to
each wall -- a 0.10 mm track fits at 0.20 mm clearance, the 0.40 mm class
width (and the 0.25 mm neck floor) do not.  A 0.10 mm kept track already runs
out of the corridor to open board at x = 6.5.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_tools.router.lattice.own_copper import KeptAnchor, kept_copper_anchors, pad_key
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

_OUTLINE = [(0.0, 0.0), (12.0, 0.0), (12.0, 8.0), (0.0, 8.0)]
_CLASS = NetClassRouting(name="SENSE", trace_width=0.4, clearance=0.2)
_NARROW = 0.1


def _pad(x, y, net, ref, *, w=0.3, h=0.3, pin="1") -> Pad:
    return Pad(
        x=x,
        y=y,
        width=w,
        height=h,
        net=net,
        net_name=f"N{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _board():
    pin = _pad(2.0, 4.0, 1, "U1")
    wall_a = _pad(3.5, 3.4, 2, "W1", w=4.0, h=0.6)
    wall_b = _pad(3.5, 4.6, 3, "W2", w=4.0, h=0.6)
    far = _pad(10.0, 4.0, 1, "R2")
    pads = [pin, wall_a, wall_b, far]
    pf = LatticePathfinder(
        _OUTLINE,
        pads,
        DesignRules(trace_width=0.25, trace_clearance=0.2),
        layer_stack=LayerStack.two_layer(),
    )
    return pf, pin, far


def _kept_route() -> Route:
    route = Route(net=1, net_name="N1")
    route.segments.append(
        Segment(x1=2.0, y1=4.0, x2=6.5, y2=4.0, width=_NARROW, layer=Layer.F_CU, net=1)
    )
    return route


def test_pin_cannot_be_re_escaped_at_class_width() -> None:
    """Precondition / defect: from the pad centre the class width has no way out."""
    pf, pin, far = _board()
    assert pf.route(pin, far, net_class=_CLASS) is None


def test_link_starts_from_kept_copper_not_the_pad() -> None:
    pf, pin, far = _board()
    anchors = kept_copper_anchors([pin, far], [_kept_route()], lambda layer: 0)
    assert anchors == {pad_key(pin): [KeptAnchor(0, (6.5, 4.0), _NARROW)]}
    pf.kept_anchors = anchors

    route = pf.route(pin, far, net_class=_CLASS)

    assert route is not None
    # Nothing is emitted over the pin or inside the corridor (walls end at
    # x = 5.5): the route begins out on open board, at the kept track's end.
    assert min(min(s.x1, s.x2) for s in route.segments) >= 6.0
    starts = [(s.x1, s.y1) for s in route.segments] + [(s.x2, s.y2) for s in route.segments]
    assert any(abs(x - 6.5) < 0.2 and abs(y - 4.0) < 0.2 for x, y in starts)
    # The new copper widens back to the class width past the narrow start.
    assert any(abs(s.width - 0.4) < 1e-9 for s in route.segments)
    # ...and it reaches the far pad.
    ends = [(s.x2, s.y2) for s in route.segments] + [(s.x1, s.y1) for s in route.segments]
    assert any(abs(x - far.x) < 1e-6 and abs(y - far.y) < 1e-6 for x, y in ends)


def test_pad_that_can_escape_is_not_rerouted_from_anchors() -> None:
    """A pad whose own escape has egress keeps its pre-#9636 stubs."""
    pf, pin, far = _board()
    pf.kept_anchors = kept_copper_anchors([pin, far], [_kept_route()], lambda layer: 0)
    # ``far`` has no kept copper -> no anchors -> identical to the plain escape.
    plain, plain_w = pf._escape_stubs_at_pad(
        far,
        1,
        None,
        kmax=4,
        extra_clearance=0.0,
        partner_net=None,
        layers=None,
        exempt_pads=None,
        net_class=_CLASS,
    )
    got, got_w = pf._escape_stubs(
        far,
        1,
        None,
        kmax=4,
        extra_clearance=0.0,
        partner_net=None,
        layers=None,
        exempt_pads=None,
        net_class=_CLASS,
    )
    assert (got, got_w) == (plain, plain_w)


def test_anchors_follow_copper_across_a_via_and_skip_the_pad_itself() -> None:
    pin = _pad(1.0, 1.0, 1, "U1")
    other = _pad(5.0, 1.0, 1, "U2")
    layers = {Layer.F_CU: 0, Layer.B_CU: 1}
    route = Route(net=1, net_name="N1")
    route.segments.append(
        Segment(x1=1.0, y1=1.0, x2=2.0, y2=1.0, width=0.15, layer=Layer.F_CU, net=1)
    )
    route.segments.append(
        Segment(x1=2.0, y1=1.0, x2=2.0, y2=3.0, width=0.2, layer=Layer.B_CU, net=1)
    )
    route.vias.append(SimpleNamespace(x=2.0, y=1.0))
    got = kept_copper_anchors([pin, other], [route], layers.get)
    assert [(a.layer, a.point, a.width) for a in got[pad_key(pin)]] == [
        (0, (2.0, 1.0), 0.15),
        (1, (2.0, 1.0), 0.2),
        (1, (2.0, 3.0), 0.2),
    ]
    # ``other`` has no copper on it.
    assert pad_key(other) not in got


# -- (b) a two-pad part whose pads sit in classes with different clearances ----


def _two_pad_part(floor: float):
    """0805-like part: R1.1 (net 1, to route) beside R1.2 (net 2, HV class)."""
    pads = [
        _pad(4.0, 4.0, 1, "R1", w=1.0, h=1.3, pin="1"),
        _pad(5.825, 4.0, 2, "R1", w=1.0, h=1.3, pin="2"),
        _pad(10.0, 4.0, 1, "R9", w=1.0, h=1.3, pin="1"),
    ]
    rules = DesignRules(trace_width=0.2, trace_clearance=0.2)
    if floor:
        rules.net_clearance_floors = {2: floor}
    pf = LatticePathfinder(
        [(0.0, 0.0), (14.0, 0.0), (14.0, 8.0), (0.0, 8.0)],
        pads,
        rules,
        layer_stack=LayerStack.two_layer(),
    )
    return pf, pads


_LOW = NetClassRouting(name="LV", trace_width=0.2, clearance=0.2)


def _fine_pitch_part():
    """Seven 0.25 x 1.0 mm U1 pads at 0.4 mm pitch (outline gap 0.15 mm).

    The middle pin (net 1) is routed to a pad 3 mm below; the board
    clearance (0.2 mm) is wider than the part's own pad gap (0.15 mm), so a
    class trace cannot leave the pin between its neighbours at the board
    rule -- main declines it and the sibling relaxation may not un-decline
    it below that rule.
    """
    pads = []
    nets = [2, 3, 4, 1, 5, 6, 7]
    for k in range(7):
        pads.append(_pad(4.0 + 0.4 * k, 4.0, nets[k], "U1", w=0.25, h=1.0, pin=str(k + 1)))
    pads.append(_pad(5.2, 1.0, 1, "R9", w=0.6, h=0.6))
    pf = LatticePathfinder(
        [(0.0, 0.0), (12.0, 0.0), (12.0, 8.0), (0.0, 8.0)],
        pads,
        DesignRules(trace_width=0.2, trace_clearance=0.2),
        layer_stack=LayerStack.two_layer(),
    )
    return pf, pads


def test_fine_pitch_sibling_floor_keeps_board_clearance() -> None:
    """A pad gap narrower than the board clearance is not a relaxation floor.

    Pre-fix the floor was ``half + gap`` (0.1 + 0.15 = 0.25 mm), below the
    siblings' own base keep-outs, so a 0.2 mm class trace shipped with its
    edge 0.175 mm from the neighbouring pins.  The floor is bounded below by
    the board clearance, so the relaxation stays off and the route declines
    exactly as main does.
    """
    from kicad_tools.router.clearance_shapes import pad_kernel_params
    from kicad_tools.router.lattice import kernel_adapter as ka

    pf, pads = _fine_pitch_part()
    pin, far = pads[3], pads[7]
    # No sibling is listed: the base-bounded floor (0.1 + 0.2 = 0.3 mm)
    # reaches as far as the siblings' own unclassed keep-outs.
    assert pf.obstacles.footprint_sibling_floors(pin, 1, 0.1, 0.0) == {}
    route = pf.route(pin, far, net_class=_LOW)
    if route is not None:
        base = pf.rules.trace_clearance
        for sib in pads[:7]:
            if sib.net == 1:
                continue
            shape, rratio = pad_kernel_params(sib)
            outline = ka.pad_outline(
                shape, sib.width, sib.height, rratio, sib.rotation, (sib.x, sib.y)
            )
            for seg in route.segments:
                if seg.layer != Layer.F_CU:
                    continue
                copper = ka.trace((seg.x1, seg.y1), (seg.x2, seg.y2), seg.width / 2)
                assert ka.gap(copper, outline) >= base - 1e-9


def test_sibling_pad_class_keepout_does_not_wall_the_pad_in() -> None:
    """A 2 mm class clearance on the sibling's net used to decline ``pad-escape-start``."""
    pf, pads = _two_pad_part(2.0)
    result, reason = pf._route_impl(
        pads[0], pads[2], _LOW, committed=pf._fresh_committed(), history={}, present=0.0
    )
    assert reason == "ok" and result is not None
    # Every same-layer copper shape keeps at least the board's base clearance
    # (0.2 mm) from the sibling pad's outline -- not merely staying outside
    # the pad.  The relaxation waives the sibling's 2 mm class floor, never
    # the board rule underneath it.
    from kicad_tools.router.clearance_shapes import pad_kernel_params
    from kicad_tools.router.lattice import kernel_adapter as ka

    sib = pads[1]
    shape, rratio = pad_kernel_params(sib)
    outline = ka.pad_outline(shape, sib.width, sib.height, rratio, sib.rotation, (sib.x, sib.y))
    base = pf.rules.trace_clearance
    for seg in result.route.segments:
        if seg.layer != Layer.F_CU:
            continue
        copper = ka.trace((seg.x1, seg.y1), (seg.x2, seg.y2), seg.width / 2)
        assert ka.gap(copper, outline) >= base - 1e-9
    for via in result.route.vias:
        if Layer.F_CU not in via.layers:
            continue
        assert ka.gap(ka.site((via.x, via.y), via.diameter / 2.0), outline) >= base - 1e-9


def test_sibling_relaxation_leaves_other_pads_unchanged() -> None:
    """The relaxation is footprint-scoped: a foreign pad's class keep-out still holds."""
    pf, pads = _two_pad_part(2.0)
    foreign = Pad(**{**pads[1].__dict__, "ref": "R2"})  # a DIFFERENT part's pad
    pf2 = LatticePathfinder(
        [(0.0, 0.0), (14.0, 0.0), (14.0, 8.0), (0.0, 8.0)],
        [pads[0], foreign, pads[2]],
        pf.rules,
        layer_stack=LayerStack.two_layer(),
    )
    assert pf.obstacles.footprint_sibling_floors(pads[0], 1, 0.1, 0.1)
    assert pf2.obstacles.footprint_sibling_floors(pads[0], 1, 0.1, 0.1) == {}
    assert pf2.route(pads[0], pads[2], net_class=_LOW) is None


def test_no_authored_clearance_leaves_escape_stubs_untouched() -> None:
    """Without a class keep-out in the way the pad's own stubs are returned as-is."""
    pf, pads = _two_pad_part(0.0)
    kwargs = {
        "kmax": 4,
        "extra_clearance": 0.0,
        "partner_net": None,
        "layers": None,
        "exempt_pads": None,
        "net_class": _LOW,
    }
    assert pf._escape_stubs(pads[0], 1, None, **kwargs) == pf._escape_stubs_at_pad(
        pads[0], 1, None, **kwargs
    )


# -- the stuck classifier: a part's own other pad is not a wall ------------------

_PCB_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
"""


def _divider_board(*, sibling_track_end: tuple[float, float]) -> str:
    """0805 R5 (pads 1.825 mm apart): R5.2 = TGT (stranded), R5.1 = SIB (routed).

    SIB is a complete two-pad net: a track from R5.1 to R6.1.  ``sibling_track_end``
    is where that track's far end sits -- running away from the stranded pad,
    or past it.
    """
    ex, ey = sibling_track_end
    return (
        _PCB_HEADER
        + '  (net 0 "")\n  (net 1 "TGT")\n  (net 2 "SIB")\n'
        # TGT island elsewhere
        + '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
        '    (property "Reference" "R1")\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n"
        # the 0805: pad 1 = SIB at x=48.2, pad 2 = TGT at x=50.0 (pad pitch 1.825 -> 50.025)
         + '  (footprint "R_0805" (layer "F.Cu") (at 49.1125 50)\n'
        '    (property "Reference" "R5")\n'
        '    (pad "1" smd rect (at -0.9125 0) (size 1.0 1.3) (layers "F.Cu") (net 2 "SIB"))\n'
        '    (pad "2" smd rect (at 0.9125 0) (size 1.0 1.3) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n"
        # the far end of the SIB track
         + f'  (footprint "R_0402" (layer "F.Cu") (at {ex} {ey})\n'
        '    (property "Reference" "R6")\n'
        '    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 2 "SIB"))\n'
        "  )\n"
        + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        + f'  (segment (start 48.2 50) (end {ex} {ey}) (width 0.25) (layer "F.Cu") (net 2))\n'
        ")\n"
    )


def _classify(text: str, tmp_path, **kwargs):
    from kicad_tools.router.stuck_classifier import classify_stuck_nets

    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    return next(d for d in classify_stuck_nets(path, **kwargs).diagnoses if d.net_name == "TGT")


def test_sibling_pad_track_is_not_a_blocker(tmp_path) -> None:
    """The only 'strict copper' near R5.2 is the track on R5.2's own part."""
    from kicad_tools.router.stuck_classifier import StuckClass

    d = _classify(_divider_board(sibling_track_end=(44.0, 50.0)), tmp_path)
    assert "SIB" not in d.blocking_nets
    assert d.classification is not StuckClass.CONGESTION_SATURATED
    # ...and the nearest-blocker evidence no longer reports the 1.825 mm pad pitch.
    assert d.nearest_blocker_mm is None or abs(d.nearest_blocker_mm - 1.825) > 0.01


def test_foreign_track_past_the_pad_is_still_a_blocker(tmp_path) -> None:
    """The exemption is only for copper that is nearest at the sibling pad."""
    from kicad_tools.router.stuck_classifier import StuckClass

    # The SIB track now runs by R5.2 itself (0.4 mm above its centre): real copper.
    d = _classify(_divider_board(sibling_track_end=(53.0, 50.4)), tmp_path)
    assert "SIB" in d.blocking_nets
    assert d.classification is StuckClass.CONGESTION_SATURATED


def test_geometric_refusal_names_width_tried_and_width_that_fits(tmp_path) -> None:
    """A refusal for trace width reports both numbers (issue #9636)."""
    from kicad_tools.router.stuck_classifier import StuckClass

    text = (
        _PCB_HEADER
        + '  (net 0 "")\n  (net 1 "TGT")\n  (net 2 "A")\n  (net 3 "B")\n'
        + '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
        '    (property "Reference" "R1")\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "TGT"))\n'
        "  )\n"
        # stranded pin in a corridor between two long foreign pads: 0.3 mm from the
        # pin centre to each wall edge, so a trace at 0.2 mm clearance can be at
        # most 2 * (0.3 - 0.2) = 0.2 mm wide
         + '  (footprint "U_X" (layer "F.Cu") (at 50 50)\n'
        '    (property "Reference" "U1")\n'
        '    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 1 "TGT"))\n'
        '    (pad "2" smd rect (at 0 -0.6) (size 8.0 0.6) (layers "F.Cu") (net 2 "A"))\n'
        '    (pad "3" smd rect (at 0 0.6) (size 8.0 0.6) (layers "F.Cu") (net 3 "B"))\n'
        "  )\n" + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        ")\n"
    )
    klass = NetClassRouting(name="SENSE", trace_width=0.4, clearance=0.2)
    d = _classify(text, tmp_path, net_class_map={"TGT": klass}, search_failures={"TGT": "no_path"})
    assert d.classification is StuckClass.ESCAPE_BLOCKED
    assert "width tried 0.400 mm" in d.evidence
    assert "widest trace that leaves the pad" in d.evidence
    import re

    fits = float(re.search(r"leaves the pad ([0-9.]+) mm", d.evidence).group(1))
    assert fits == pytest.approx(0.2, abs=0.01)
    assert fits < 0.4, "the width that fits is narrower than the width that was tried"
