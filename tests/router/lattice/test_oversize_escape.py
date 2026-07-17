"""Tapered (neck-down) pad escape for oversize net classes (issue #4293).

Oversize copper (e.g. 2.6 mm HV_HICUR) carries a large keep-out surcharge
(``half + clearance - agent_radius``) on every static pad check, so in a
dense fuse/terminal pad field NO full-width dogleg clears and the connection
declines honestly (``pad-escape-*``).  The fix emits the pad-escape legs at a
narrower LEGAL neck width and widens back to the class width at the first
lattice node -- standard heavy-copper practice.  These tests pin:

* the full-width escape is byte-identical for anything that already escapes
  (the neck only fires on a genuine full-width decline);
* an oversize pad buried in a dense field escapes via the neck taper;
* the neck legs are clearance-checked at the NECK width (never-ship-a-short);
* the widened body is emitted AND spaced at the full class width, and the
  committed-copper model records each segment at its EMITTED width (the taper
  does not cheat the #4289 spacing math);
* the neck width is a principled legal floor, not a magic constant.
"""

from __future__ import annotations

from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

_HV = NetClassRouting(name="HV_HICUR", trace_width=2.6, clearance=0.3)


def _pad(x: float, y: float, net: int, ref: str, *, w: float = 0.6, h: float = 0.6) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=w,
        height=h,
        net=net,
        net_name=f"N{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _dense_field() -> tuple[LatticePathfinder, Pad, Pad]:
    """An oversize net-1 pad boxed on three sides by an other-net pad field,
    with an open west corridor to a second net-1 pad.

    At the full 2.6 mm width the keep-out surcharge (~1.45 mm) covers every
    reachable lattice node, so the pad cannot escape; a 0.2 mm neck clears the
    open corridor and the body widens west of the field.
    """
    pads = [_pad(8.0, 10.0, 1, "U1")]  # the buried oversize pad
    i = 0
    for dx in (1.2, 2.0, 2.8):
        for dy in (-2.0, -1.2, -0.6, 0.6, 1.2, 2.0):
            pads.append(_pad(8.0 + dx, 10.0 + dy, 2, f"E{i}"))
            i += 1
    for dx in (-0.4, 0.4, 1.0):
        pads.append(_pad(8.0 + dx, 10.0 - 1.6, 2, f"S{i}"))
        pads.append(_pad(8.0 + dx, 10.0 + 1.6, 2, f"N{i}"))
        i += 1
    pads.append(_pad(2.0, 10.0, 1, "U2"))  # far west sink (open)
    pf = LatticePathfinder(
        [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)],
        pads,
        DesignRules(),
        layer_stack=LayerStack.two_layer(),
    )
    return pf, pads[0], pads[-1]


def test_full_width_escape_declines_but_neck_taper_escapes() -> None:
    pf, buried, _sink = _dense_field()

    # Full class width: the surcharge blocks every dogleg -> honest decline.
    assert pf.pad_stubs(buried, net=1, net_class=_HV) == []

    # The tapered fallback finds neck stubs at the neck width.
    stubs, width = pf._escape_stubs(
        buried,
        1,
        None,
        kmax=4,
        extra_clearance=0.0,
        partner_net=None,
        layers=None,
        exempt_pads=None,
        net_class=_HV,
    )
    assert stubs, "neck taper must find a legal escape the full width could not"
    assert width == 0.2, "neck width is the DRU floor (rules.trace_width)"


def test_neck_legs_are_clearance_checked_at_the_neck_width() -> None:
    """Never-ship-a-short (#3906): every emitted neck leg clears other-net pad
    keep-outs when checked at the NECK half-width surcharge."""
    pf, buried, _sink = _dense_field()
    obstacles = pf.obstacles
    neck_half = 0.1  # 0.2 mm neck
    clr = 0.3
    extra = max(0.0, neck_half + clr - pf._agent_radius)

    stubs, _w = pf._escape_stubs(
        buried,
        1,
        None,
        kmax=4,
        extra_clearance=0.0,
        partner_net=None,
        layers=None,
        exempt_pads=None,
        net_class=_HV,
    )
    assert stubs
    for _key, layer, poly, _length in stubs:
        assert poly[0] == (buried.x, buried.y), "escape starts at the exact pad"
        for a, b in zip(poly, poly[1:], strict=False):
            assert not obstacles.segment_blocked(a, b, layer, net=1, extra=extra), (
                f"neck leg {a}->{b} crosses an other-net keep-out at the neck width"
            )


def test_route_emits_neck_escape_and_full_width_body() -> None:
    pf, buried, sink = _dense_field()
    route = pf.route(buried, sink, net_class=_HV)
    assert route is not None, "the oversize net must connect via the neck taper"

    widths = sorted({round(s.width, 4) for s in route.segments})
    assert widths == [0.2, 2.6], f"expected neck+body widths, got {widths}"

    # The exact pad-touching segment is the neck; the far body is full width.
    touching = [
        s
        for s in route.segments
        if (abs(s.x1 - buried.x) < 1e-6 and abs(s.y1 - buried.y) < 1e-6)
        or (abs(s.x2 - buried.x) < 1e-6 and abs(s.y2 - buried.y) < 1e-6)
    ]
    assert touching, "a segment must touch the buried pad exactly"
    assert all(abs(s.width - 0.2) < 1e-9 for s in touching), "pad-touching leg is the neck"
    assert any(abs(s.width - 2.6) < 1e-9 for s in route.segments), "body widens to class width"


def test_default_width_net_escapes_at_full_width_no_taper() -> None:
    """A net at the board default width escapes at full width -- the neck
    fallback never fires, so behavior is byte-identical to pre-#4293."""
    pads = [_pad(10.0, 10.0, 1, "U1"), _pad(4.0, 10.0, 1, "U2")]
    pf = LatticePathfinder(
        [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)],
        pads,
        DesignRules(),
        layer_stack=LayerStack.two_layer(),
    )
    stubs, width = pf._escape_stubs(
        pads[0],
        1,
        None,
        kmax=4,
        extra_clearance=0.0,
        partner_net=None,
        layers=None,
        exempt_pads=None,
        net_class=None,
    )
    assert stubs
    assert width == pf.rules.trace_width, "default net keeps the full width (no taper)"
    route = pf.route(pads[0], pads[1])
    assert route is not None
    assert {round(s.width, 4) for s in route.segments} == {pf.rules.trace_width}


def test_committed_copper_spaces_each_segment_at_its_emitted_width() -> None:
    """The #4289 spacing model must use the ACTUAL emitted width per segment:
    a neck leg is spaced as neck copper, the widened body as full-width copper.
    The taper must not let the body cheat the spacing math."""
    committed = CommittedCopper(
        num_layers=2,
        trace_half=0.1,
        clearance=0.2,
        via_radius=0.35,
        via_via_gap=0.9,
        same_net_via_gap=0.85,
    )
    # A neck leg (half 0.1) then a widened body (half 1.3) on net 1.
    committed.add_run_widths(
        0, [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)], net=1, seg_halves=[0.1, 1.3], clearance=0.3
    )

    # An other-net (2) probe with a 0.1 mm half-width, 0.3 clearance.
    # Gap to the neck seg  = 0.1 + 0.1 + 0.3 = 0.5 mm.
    # Gap to the body seg  = 0.1 + 1.3 + 0.3 = 1.7 mm.
    near_neck = (2.5, 0.6)  # 0.6 mm off the neck seg -> clears (>= 0.5)
    near_body = (7.5, 0.6)  # 0.6 mm off the body seg -> too close (< 1.7)
    assert committed.node_clear(near_neck, 0, net=2, half=0.1, clearance=0.3)
    assert not committed.node_clear(near_body, 0, net=2, half=0.1, clearance=0.3)


def test_neck_width_is_a_legal_floor_and_class_can_override() -> None:
    pads = [_pad(10.0, 10.0, 1, "U1")]
    pf = LatticePathfinder(
        [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)],
        pads,
        DesignRules(min_trace_width=0.15),
        layer_stack=LayerStack.two_layer(),
    )
    # Default: the DRU min-trace floor (0.15), never below it.
    assert pf._neck_width(_HV) == 0.15
    # A class may raise the neck for ampacity; still floored at the fab min.
    wide_neck = NetClassRouting(name="HV", trace_width=2.6, neck_trace_width=1.0)
    assert pf._neck_width(wide_neck) == 1.0
    thin_neck = NetClassRouting(name="HV", trace_width=2.6, neck_trace_width=0.05)
    assert pf._neck_width(thin_neck) == 0.15, "override below the fab min is clamped up"


def test_route_netset_converts_oversize_decline_with_zero_short() -> None:
    """End-to-end: the negotiation routes the oversize net via the neck taper
    and the emitted copper has no cross-net short at the per-class gaps."""
    pf, buried, sink = _dense_field()
    conns = [((1, 0), buried, sink, _HV)]
    routes, stats = pf.route_netset(conns, max_iterations=2)

    assert stats.lattice_builds == 1
    assert (1, 0) in routes, f"oversize net must route; declines={pf.failure_reasons}"
    route = routes[(1, 0)]
    assert {round(s.width, 4) for s in route.segments} == {0.2, 2.6}

    # No emitted segment intrudes on an other-net pad keep-out at its width.
    obstacles = pf.obstacles
    for seg in route.segments:
        layer = pf.layer_stack.layer_enum_to_index(seg.layer)
        extra = max(0.0, seg.width / 2.0 + 0.3 - pf._agent_radius)
        assert not obstacles.segment_blocked(
            (seg.x1, seg.y1), (seg.x2, seg.y2), layer, net=1, extra=extra
        ), "emitted copper crosses an other-net pad keep-out"
