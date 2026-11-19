"""Issue #9018: lattice legs keep out of the same-net pad slit band.

#9011 measured board 07's 14 same-net ``physical_copper_gap`` findings as the
lattice engine's own escape geometry (pad-stub doglegs, attach doglegs and
offset pair legs) running parallel to an own-net pad edge inside the fab
``pad_spacing`` band: same-net copper is exempt from every other
leg-legality predicate, so nothing looked at those pads.  This file unit-tests
the enforcement surface the fix added -- the exact-outline two-region
predicate on :class:`LatticeObstacleModel` and every leg-acceptance site that
now calls it -- with an own-net pad and a leg inside vs outside the band:

* the predicate itself: band-interior rejected, pad-union and at/beyond the
  detection edge accepted, PTH pads / other layers / other nets exempt, the
  guard off at ``edge_mm <= 0``, and the exact rotated outline (never the
  grown bounding rectangle) deciding a diagonal skirt;
* the stub scan (``pad_stubs``): a stub whose dogleg leg runs in-band beside
  a second same-net pad is refused with the guard on;
* the A* body predicate (``_body_edge_legal``): a lattice edge threading the
  band beside an own-net pad (invisible to the foreign-only masks) is
  refused; one beyond the edge is not;
* the coupled attach (``_pair_attach``): a target both of whose dogleg
  candidates skirt own-net pads in-band declines, naming the band;
* the gateway fan gate (``_gateway_fan_ok``): an in-band fan leg is refused;
* the coupled finish gate, end to end: a routed pair's emitted legs (post
  ``clip_to_pad_entry``) never sit in the band beside their own pads, and the
  pair still couples (the completion floor).

The board-07 population gate (14 findings -> 0) lives in
``scripts/ci/check_matchgroup_coverage.py`` and is re-measured by that CI
gate, not here.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

sys.path.insert(0, str(Path(__file__).parent))
from test_physical_copper_gap import board, gaps  # noqa: E402

EDGE = 0.15  # jlcpcb 4layer pad_spacing detection edge (#9016)
HALF = 0.1  # 0.2 mm trace, the board default in these fixtures

_OUTLINE = [(-2.0, -2.0), (12.0, -2.0), (12.0, 6.0), (-2.0, 6.0)]


def _rules(edge: float | None) -> DesignRules:
    return DesignRules(
        trace_width=2 * HALF,
        trace_clearance=0.15,
        manufacturer="jlcpcb",
        same_net_pad_gap_band_edge_mm=edge,
    )


def _pad(
    x: float,
    y: float,
    net: int = 1,
    net_name: str = "GND",
    *,
    ref: str = "U1",
    width: float = 0.6,
    height: float = 0.6,
    rotation: float = 0.0,
    drill: float = 0.0,
    through_hole: bool = False,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
        rotation=rotation,
        drill=drill,
        through_hole=through_hole,
    )


def _pf(pads: list[Pad], edge: float | None = EDGE, **kw) -> LatticePathfinder:
    return LatticePathfinder(_OUTLINE, pads, _rules(edge), LayerStack.two_layer(), **kw)


def _band_blocked(pf: LatticePathfinder, a, b, layer: int = 0, net: int = 1) -> bool:
    return pf.obstacles.same_net_pad_band_blocked(a, b, layer, net, HALF, EDGE)


# ---------------------------------------------------------------------------
# The predicate: two regions, exact outline (#9012 semantics, lattice form)
# ---------------------------------------------------------------------------


def test_band_interior_parallel_leg_is_blocked() -> None:
    # Pad 0.6x0.6 at the origin: bottom edge y = -0.3.  A parallel leg at
    # y = -0.5 keeps 0.2 mm centreline -> 0.1 mm copper gap, inside (0, 0.15).
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    assert _band_blocked(pf, (-1.5, -0.5), (1.5, -0.5))


def test_pad_union_leg_is_accepted() -> None:
    # 0.05 mm centreline standoff: the swept copper overlaps the pad -- one
    # copper union, no slit.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    assert not _band_blocked(pf, (-1.5, -0.35), (1.5, -0.35))
    # A leg running to the pad centre crosses the outline: union as well.
    assert not _band_blocked(pf, (0.0, 0.0), (0.0, -2.0))


def test_leg_at_and_beyond_the_detection_edge_is_accepted() -> None:
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    # Exactly at the edge (0.25 mm centreline): the band is open at its top.
    assert not _band_blocked(pf, (-1.5, -0.55), (1.5, -0.55))
    assert not _band_blocked(pf, (-1.5, -0.8), (1.5, -0.8))


def test_guard_off_when_edge_unresolved() -> None:
    pf = _pf([_pad(0.0, 0.0)], edge=None)
    pf.build()
    assert not pf.obstacles.same_net_pad_band_blocked((-1.5, -0.5), (1.5, -0.5), 0, 1, HALF, 0.0)
    assert pf._own_net_pad_band_ok((-1.5, -0.5), (1.5, -0.5), 0, 1, HALF)


def test_through_hole_pad_is_not_band_checked() -> None:
    # The #9012 predicate's SMD scope: a drilled neighbour is the
    # hole-to-hole floor's business, never the slit band's.
    pf = _pf([_pad(0.0, 0.0, drill=0.3, through_hole=True)])
    pf.build()
    assert not _band_blocked(pf, (-1.5, -0.5), (1.5, -0.5))


def test_other_layer_and_other_net_pads_are_exempt() -> None:
    pf = _pf([_pad(0.0, 0.0, net=2, net_name="VCC")])
    pf.build()
    assert not _band_blocked(pf, (-1.5, -0.5), (1.5, -0.5))
    # The same pad probed on B.Cu (layer 1 of the two-layer stack).
    assert not pf.obstacles.same_net_pad_band_blocked((-1.5, -0.5), (1.5, -0.5), 1, 2, HALF, EDGE)


def test_rotated_pad_measures_the_true_outline_not_the_bbox() -> None:
    # A 45-degree-rotated square pad is a diamond with axis vertices at
    # +-(0.3*sqrt(2)).  A diagonal leg on the line x+y = 1.05 measures
    # (1.05 - 0.3*sqrt(2)) / sqrt(2) ~ 0.443 mm to the diamond's face -- well
    # beyond the edge -- while its axis-aligned bbox sits ~0.142 mm from the
    # leg.  A bbox-grown predicate would reject the leg; the exact outline
    # accepts it.
    pf = _pf([_pad(0.0, 0.0, rotation=45.0)])
    pf.build()
    assert not _band_blocked(pf, (0.3, 0.75), (0.75, 0.3))
    # The same probe one band inside the diamond's face IS rejected.
    c = 0.3 * math.sqrt(2) + math.sqrt(2) * (HALF + EDGE / 2.0)
    a = (c / 2 - 0.3, c / 2 + 0.3)
    b = (c / 2 + 0.3, c / 2 - 0.3)
    assert _band_blocked(pf, a, b)


def test_point_probe_form() -> None:
    # The degenerate a == b form measures the swept disc's edge, so a node
    # site in the band is refused exactly like a leg through it.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    assert _band_blocked(pf, (0.0, -0.5), (0.0, -0.5))
    assert not _band_blocked(pf, (0.0, -0.6), (0.0, -0.6))


# ---------------------------------------------------------------------------
# Stub scan legs (pathfinder._scan_stubs, the non-fat emitted path)
# ---------------------------------------------------------------------------


def _stub_in_band(pf: LatticePathfinder, stub, net: int) -> bool:
    """True when some same-net pad sees a stub leg inside the open band.

    Measured per pad (the origin pad unions every candidate by construction;
    the question is whether ANY pad -- origin or neighbour -- is skirted).
    """
    for idx, pad in enumerate(pf.pads):
        if pad.net != net or 0 not in pf.obstacles.pad_layer_indices[idx]:
            continue
        worst = math.inf
        for a, b in zip(stub[2], stub[2][1:], strict=False):
            worst = min(worst, pf.obstacles.pad_centerline_gap(idx, a, b) - HALF)
        if 1e-9 < worst < EDGE - 1e-9:
            return True
    return False


def test_stub_scan_refuses_in_band_dogleg_legs() -> None:
    # Two same-net pads 0.9 mm apart: the 0.3 mm slot between them is exactly
    # where an unguarded dogleg's second leg loves to run -- parallel, inside
    # the band, unioned with neither pad.
    pad_a = _pad(5.0, 5.0, ref="U1")
    pad_b = _pad(5.0, 5.9, ref="U1")
    open_pf = _pf([pad_a, pad_b], edge=None, fine=0.1)
    guarded_pf = _pf([pad_a, pad_b], edge=EDGE, fine=0.1)
    open_stubs = open_pf.pad_stubs(pad_a, 1, kmax=128)
    guarded_stubs = guarded_pf.pad_stubs(pad_a, 1, kmax=128)
    assert open_stubs, "fixture must offer the unguarded scan at least one stub"
    # The unguarded scan accepts at least one stub with a leg inside the band
    # (0 < gap < EDGE) beside a same-net pad -- the fixture's premise.
    assert any(_stub_in_band(open_pf, s, 1) for s in open_stubs)
    # The guarded scan never does.
    assert not any(_stub_in_band(guarded_pf, s, 1) for s in guarded_stubs)


# ---------------------------------------------------------------------------
# A* body edges (_body_edge_legal): own-net pads are invisible to the masks
# ---------------------------------------------------------------------------


def _lattice_edges(pf: LatticePathfinder):
    lattice = pf.build()
    for edge in lattice.edges:
        yield edge, lattice.node_point(edge[0]), lattice.node_point(edge[1])


def test_body_edge_legal_refuses_band_interior_edge() -> None:
    # A wide pad gives the band a long horizontal strip to thread; with the
    # foreign-only pad masks blind to same-net pads, the lattice happily
    # places body edges there.  The band guard must refuse them.
    pad = _pad(5.0, 0.0, width=3.0, height=0.6, ref="U1")
    open_pf = _pf([pad], edge=None, fine=0.1)
    guarded_pf = _pf([pad], edge=EDGE, fine=0.1)
    committed = guarded_pf._fresh_committed()
    in_band = []
    for edge, a, b in _lattice_edges(guarded_pf):
        gap = guarded_pf.obstacles.pad_centerline_gap(0, a, b) - HALF
        if 1e-9 < gap < EDGE - 1e-9 and abs(a[1] - b[1]) < 1e-9:
            in_band.append((edge, a, b))
    assert in_band, "fixture must place at least one lattice edge inside the band"
    for edge, _a, _b in in_band:
        assert not guarded_pf._body_edge_legal(
            edge, 0, 1, HALF, 0.15, committed, extra=0.0, pw=None, ko=None
        )[0]
        assert open_pf._body_edge_legal(
            edge, 0, 1, HALF, 0.15, open_pf._fresh_committed(), extra=0.0, pw=None, ko=None
        )[0]


def test_body_edge_legal_accepts_edge_beyond_the_band() -> None:
    pad = _pad(5.0, 0.0, width=3.0, height=0.6, ref="U1")
    pf = _pf([pad], edge=EDGE, fine=0.1)
    committed = pf._fresh_committed()
    found = 0
    for edge, a, b in _lattice_edges(pf):
        if abs(a[1] - b[1]) > 1e-9:
            continue
        gap = pf.obstacles.pad_centerline_gap(0, a, b) - HALF
        if gap >= EDGE + 0.05:
            assert pf._body_edge_legal(
                edge, 0, 1, HALF, 0.15, committed, extra=0.0, pw=None, ko=None
            )[0]
            found += 1
    assert found


# ---------------------------------------------------------------------------
# The #9517 exemption: a leg riding a pad-attached stub is the validator's
# covered connector, not a slit
# ---------------------------------------------------------------------------


def test_outward_collinear_continuation_of_pad_attached_stub_is_accepted() -> None:
    # Pad 0.6x0.6 at the origin; its stub tail runs straight down from the
    # pad centre to (0.0, -0.42) -- 0.12 mm past the bottom edge (y = -0.3).
    # A continuation leg from there to (0.0, -0.52) parks in the band
    # (centreline gap 0.02..0.12) yet is exactly the #9517 shape: on the
    # tail's line, its start riding the tail's swept copper, the tail
    # unions the pad.  In isolation it is the board 05 deadlock (rejected);
    # with the stub legs attached the validator's connector-coverage
    # exemption grades the composite escape clean, and so must the guard.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    leg = ((0.0, -0.42), (0.0, -0.52))
    tail = ((0.0, 0.0), (0.0, -0.42), HALF)
    assert _band_blocked(pf, *leg)
    assert not pf.obstacles.same_net_pad_band_blocked(
        leg[0], leg[1], 0, 1, HALF, EDGE, attached=(tail,)
    )


def test_parallel_band_leg_stays_rejected_despite_attached_stub() -> None:
    # The #9011 slit shapes beside the SAME tail: a leg parallel to the pad
    # edge, a corner off the tail's end, and a collinear leg laterally
    # offset from the tail's line -- none rides the tail, so none is
    # exempt.  Board 07's slit count stays at 0.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    tail = ((0.0, 0.0), (0.0, -0.42), HALF)
    # parallel to the bottom edge, in the band
    assert _band_blocked(pf, (-0.5, -0.42), (0.5, -0.42))
    assert pf.obstacles.same_net_pad_band_blocked(
        (-0.5, -0.42), (0.5, -0.42), 0, 1, HALF, EDGE, attached=(tail,)
    )
    # a corner off the tail's end: rides it, not collinear
    assert pf.obstacles.same_net_pad_band_blocked(
        (0.0, -0.42), (0.2, -0.42), 0, 1, HALF, EDGE, attached=(tail,)
    )
    # collinear with the tail's axis but laterally offset: misses it
    assert _band_blocked(pf, (0.3, -0.42), (0.3, -0.52))
    assert pf.obstacles.same_net_pad_band_blocked(
        (0.3, -0.42), (0.3, -0.52), 0, 1, HALF, EDGE, attached=(tail,)
    )


def test_collinear_leg_back_toward_the_pad_is_accepted_on_the_tail() -> None:
    # The #9517 plan's back-toward-pad probe.  Direction-free on purpose
    # (the A* edge memo is unordered, so egress and arrival must agree): a
    # leg on the tail's line lays no copper off the pad-attached escape
    # either way, so it is the covered connector too.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    leg = ((0.0, -0.52), (0.0, -0.42))
    tail = ((0.0, 0.0), (0.0, -0.42), HALF)
    assert _band_blocked(pf, *leg)
    assert not pf.obstacles.same_net_pad_band_blocked(
        leg[0], leg[1], 0, 1, HALF, EDGE, attached=(tail,)
    )


def test_stub_end_point_probe_on_attached_copper_is_accepted() -> None:
    # The degenerate a == b form at the stub end itself: the node sits on
    # pad-attached copper, so the point probe -- no direction to collinearity
    # -- is exempt on ride + attachment alone.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    tail = ((0.0, 0.0), (0.0, -0.42), HALF)
    assert _band_blocked(pf, (0.0, -0.42), (0.0, -0.42))
    assert not pf.obstacles.same_net_pad_band_blocked(
        (0.0, -0.42), (0.0, -0.42), 0, 1, HALF, EDGE, attached=(tail,)
    )


def test_distant_parallel_band_leg_rides_nothing_even_with_attached_stub() -> None:
    # The ride test's pin (#9517 review): a band leg parallel to the tail's
    # axis but starting far along it rides NONE of the tail's copper -- both
    # endpoints are beyond ``half + hw`` of the segment (0.90 / 0.45 mm off,
    # threshold 0.2).  A ride metric that measures anything but the leg's own
    # endpoints against the ride segment grades this slit shape an escape --
    # exactly the #9011 shape the guard exists to reject.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    tail = ((0.0, 0.0), (0.0, -0.42), HALF)
    assert _band_blocked(pf, (0.45, -1.2), (0.45, -0.4))
    assert pf.obstacles.same_net_pad_band_blocked(
        (0.45, -1.2), (0.45, -0.4), 0, 1, HALF, EDGE, attached=(tail,)
    )


def test_continuation_leg_longer_than_the_ride_band_is_exempt_from_either_end() -> None:
    # Direction-free for a leg LONGER than the ride threshold that extends
    # past the stub end: its near endpoint rides the tail, its far endpoint
    # is 0.33 mm beyond it (past ``half + hw`` = 0.2).  Whichever endpoint
    # the canonical A* edge order presents as the leg's start must give the
    # same verdict -- copper on the tail's line lays no band copper either
    # way.  (A start-only ride test grades the far-end-first order a slit.)
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    tail = ((0.0, 0.0), (0.0, -0.42), HALF)
    leg = ((0.0, -0.42), (0.0, -0.75))
    assert _band_blocked(pf, *leg)
    assert not pf.obstacles.same_net_pad_band_blocked(
        leg[0], leg[1], 0, 1, HALF, EDGE, attached=(tail,)
    )
    assert not pf.obstacles.same_net_pad_band_blocked(
        leg[1], leg[0], 0, 1, HALF, EDGE, attached=(tail,)
    )


def test_attached_segment_that_misses_the_pad_exempts_nothing() -> None:
    # Attachment is what makes the composite escape validator-clean: a
    # same-net segment whose copper stops short of the outline (centreline
    # gap 0.15 > half) cannot exempt the leg that rides it.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    orphan = ((0.0, -0.45), (0.0, -0.52), HALF)
    leg = ((0.0, -0.45), (0.0, -0.52))
    assert _band_blocked(pf, *leg)
    assert pf.obstacles.same_net_pad_band_blocked(
        leg[0], leg[1], 0, 1, HALF, EDGE, attached=(orphan,)
    )


def test_rotated_pad_stub_continuation_measures_the_true_outline() -> None:
    # A 45-degree-rotated square pad is a diamond; the stub tail runs down
    # its -y axis through the vertex, and the in-band continuation rides it
    # out of the band.  Attachment and the band both measure the kernel's
    # rotated outline (#7503), never the bbox.
    pf = _pf([_pad(0.0, 0.0, rotation=45.0)])
    pf.build()
    leg = ((0.0, -0.55), (0.0, -0.6))
    tail = ((0.0, 0.0), (0.0, -0.55), HALF)
    assert _band_blocked(pf, *leg)
    assert not pf.obstacles.same_net_pad_band_blocked(
        leg[0], leg[1], 0, 1, HALF, EDGE, attached=(tail,)
    )


def test_body_edge_legal_accepts_continuation_of_attached_stub() -> None:
    # The A* predicate form: the vertical in-band edge below the pad is
    # refused without the stub legs (the #9517 deadlock) and accepted with
    # them, while the in-band parallels beside it stay refused even with
    # them attached.
    pad = _pad(5.0, 0.0, width=3.0, height=0.6, ref="U1")
    pf = _pf([pad], edge=EDGE, fine=0.1)
    committed = pf._fresh_committed()
    cont = parallel = None
    for edge, a, b in _lattice_edges(pf):
        gap = pf.obstacles.pad_centerline_gap(0, a, b) - HALF
        if not (1e-9 < gap < EDGE - 1e-9):
            continue
        if abs(a[0] - b[0]) < 1e-9 and pad.x - pad.width / 2 < a[0] < pad.x + pad.width / 2:
            cont = (edge, a, b)
        elif abs(a[1] - b[1]) < 1e-9:
            parallel = (edge, a, b)
    assert cont is not None and parallel is not None, "fixture must offer both shapes"
    (cx_edge, ca, _cb) = cont
    (px_edge, pa, _pb) = parallel
    attached = (((ca[0], 0.0), ca, HALF),)
    assert not pf._body_edge_legal(
        cx_edge, 0, 1, HALF, 0.15, committed, extra=0.0, pw=None, ko=None
    )[0]
    assert pf._body_edge_legal(
        cx_edge, 0, 1, HALF, 0.15, committed, extra=0.0, pw=None, ko=None, attached=attached
    )[0]
    assert not pf._body_edge_legal(
        px_edge, 0, 1, HALF, 0.15, committed, extra=0.0, pw=None, ko=None, attached=attached
    )[0]
    # The exemption is direction-free -- the ride test takes the NEARER of
    # the leg's two endpoints to the ride segment -- so the same vertical
    # edge discovered from its far end (the goal-side arrival order) is the
    # direction-free memo's answer too.
    assert pf._body_edge_legal(
        cx_edge, 0, 1, HALF, 0.15, committed, extra=0.0, pw=None, ko=None, attached=attached
    ) == pf._body_edge_legal(
        (cx_edge[1], cx_edge[0]),
        0,
        1,
        HALF,
        0.15,
        committed,
        extra=0.0,
        pw=None,
        ko=None,
        attached=attached,
    )


# ---------------------------------------------------------------------------
# The #9538 review pins: the ride is the SELECTED escape's own copper, on
# its own layer, at its emitted width, and collinear means ON the line
# ---------------------------------------------------------------------------


def _pad_footprint(pad: Pad) -> str:
    layer = "F.Cu" if pad.layer == Layer.F_CU else "B.Cu"
    return (
        f'(footprint "R" (layer "{layer}") (at {pad.x} {pad.y}) '
        f'(property "Reference" "{pad.ref}") (pad "1" smd rect (at 0 0) '
        f'(size {pad.width} {pad.height}) (layers "{layer}") (net 1 "GND")))'
    )


def _route_sexps(route) -> list[str]:
    items = [
        f"(segment (start {s.x1} {s.y1}) (end {s.x2} {s.y2}) (width {s.width}) "
        f'(layer "{s.layer.kicad_name}") (net 1) (uuid "s{i}"))'
        for i, s in enumerate(route.segments)
    ]
    items.extend(
        f"(via (at {v.x} {v.y}) (size 0.6) (drill 0.3) "
        f'(layers "F.Cu" "B.Cu") (net 1) (uuid "v{i}"))'
        for i, v in enumerate(route.vias)
    )
    return items


def test_route_emits_no_slit_riding_an_unselected_stub_candidate() -> None:
    # #9538 review finding 1: pooling EVERY start/goal stub candidate into
    # one ride set let an unselected candidate's copper legalise the
    # SELECTED escape's first body leg.  On this two-pad fixture the first
    # body edge (2.2, 1.0) -> (2.4, 1.2) failed the guard with the stub the
    # router emits, passed only via a candidate it does not, and the board
    # carried a 0.1245 mm same-net slit to U1.  The ride set must follow
    # the selected stub at each end of the path (and the unordered-edge
    # memo must key on it), so the search takes the collinear continuation
    # the emitted escape actually covers -- or another legal leg.
    a = _pad(2.03, 1.015, width=0.12, height=0.6)
    b = _pad(5.02, 2.05, width=0.3, height=0.6, ref="J2")
    pf = _pf([a, b], fine=0.4)
    route = pf.route(a, b)
    assert route is not None, "the connection must still route (#9517 unlock)"
    assert not route.vias
    findings = [
        (v.actual_value, v.items)
        for v in gaps(board(*map(_pad_footprint, (a, b)), *_route_sexps(route)), EDGE)
    ]
    assert findings == [], findings


def test_route_never_rides_a_foreign_layer_stub_out_of_the_band() -> None:
    # #9538 review finding 1, layer half: the pooled ride set also ignored
    # the stubs' layers, so a collinear B.Cu escape legalised F.Cu band
    # legs.  With pad b moved to B.Cu the same two-pad fixture emitted the
    # same 0.1245 mm F.Cu slit beside U1, graded clean only because a
    # foreign-layer stub sat on the line.  Rides are keyed on the edge's
    # own layer now, so the F.Cu exit must stand on F.Cu copper.
    a = _pad(2.03, 1.015, width=0.12, height=0.6)
    b = Pad(
        x=5.02,
        y=2.05,
        width=0.3,
        height=0.6,
        net=1,
        net_name="GND",
        layer=Layer.B_CU,
        ref="J2",
        pin="1",
    )
    pf = _pf([a, b], fine=0.4)
    route = pf.route(a, b)
    assert route is not None, "the connection must still route (#9517 unlock)"
    findings = [
        (v.actual_value, v.items)
        for v in gaps(board(*map(_pad_footprint, (a, b)), *_route_sexps(route)), EDGE)
    ]
    assert findings == [], findings


def test_parallel_leg_within_combined_half_widths_is_not_collinear() -> None:
    # #9538 review finding 2: a leg offset 0.15 mm sideways rides the tail
    # (contact: 0.15 <= half + hw = 0.2) and is parallel to it, yet its
    # copper sits BESIDE the pad-attached escape -- the validator measures
    # a 0.02 mm slit between the leg and the pad where the guard accepted.
    # Collinear means ON the supporting line (perpendicular distance ~ 0),
    # not merely parallel; the same leg moved onto the tail's centerline
    # (x = 0) is the accepted continuation.
    pf = _pf([_pad(0.0, 0.0)])
    pf.build()
    tail = ((0.0, 0.0), (0.0, -0.42), HALF)
    assert _band_blocked(pf, (0.15, -0.42), (0.15, -0.62))
    assert pf.obstacles.same_net_pad_band_blocked(
        (0.15, -0.42), (0.15, -0.62), 0, 1, HALF, EDGE, attached=(tail,)
    )
    assert not pf.obstacles.same_net_pad_band_blocked(
        (0.0, -0.42), (0.0, -0.62), 0, 1, HALF, EDGE, attached=(tail,)
    )


def test_stub_egress_rides_attach_at_the_stubs_emitted_width() -> None:
    # #9538 review finding 1, taper half: a neck escape's rides were graded
    # at the body radius, accepting rides the emitted neck does not cover.
    # The stub below lands with every outgoing edge in the band beside the
    # 1.4 mm pad (corner gaps inside (half, half + edge) at half = 0.1), so
    # its egress is decided entirely by the straight continuation's ride --
    # and the ride only covers the pad when the stub's own half-width
    # reaches it: 0.125 mm standoff attaches at the 0.15 body radius, not
    # at the 0.05 emitted neck.
    pf = LatticePathfinder(
        [(-1.0, -1.0), (7.0, -1.0), (7.0, 1.0), (-1.0, 1.0)],
        [_pad(5.0, 0.0, width=1.4, height=0.6)],
        _rules(EDGE),
        LayerStack.two_layer(),
        fine=0.025,
    )
    lattice = pf.build()
    stub = (lattice.node_key(5.825, -0.45), 0, [(5.825, 0.0), (5.825, -0.45)], 0.45)
    committed = pf._fresh_committed()
    assert not pf._stub_has_egress(stub, 1, committed, HALF, 0.15, allow_vias=False, stub_half=0.05)
    assert pf._stub_has_egress(stub, 1, committed, HALF, 0.15, allow_vias=False, stub_half=0.15)


# ---------------------------------------------------------------------------
# Coupled attach doglegs (_pair_attach)
# ---------------------------------------------------------------------------


def test_pair_attach_declines_when_every_candidate_skirts_the_band() -> None:
    # Pad A at the origin (net 1).  Target (0.42, -2.0): the diagonal-first
    # dogleg's landing leg runs vertical at x = 0.42 -- 0.12 mm from A's
    # edge, 0.02 mm of copper gap, inside the band.  The axis-first dogleg's
    # landing diagonal passes the second same-net pad B at (0.9, -1.79) the
    # same way.  Both candidates rejected -> the attach declines and names
    # the band.
    pad_a = _pad(0.0, 0.0, ref="U1")
    pad_b = _pad(0.9, -1.79, ref="U2")
    pf = _pf([pad_a, pad_b], edge=EDGE)
    committed = pf._fresh_committed()
    poly = pf._pair_attach(pad_a, (0.42, -2.0), 0, 1, {1}, committed, HALF, 0.15)
    assert poly is None
    assert "own-net-pad-band" in pf._attach_blocker
    # With the guard off the same attach succeeds (nothing else blocks it).
    open_pf = _pf([pad_a, pad_b], edge=None)
    poly = open_pf._pair_attach(
        pad_a, (0.42, -2.0), 0, 1, {1}, open_pf._fresh_committed(), HALF, 0.15
    )
    assert poly is not None


# ---------------------------------------------------------------------------
# Gateway fan legs (_gateway_fan_ok)
# ---------------------------------------------------------------------------


def test_gateway_fan_refuses_band_interior_leg() -> None:
    pf = _pf([_pad(0.0, 0.0)], edge=EDGE)
    committed = pf._fresh_committed()
    assert not pf._gateway_fan_ok([(-1.5, -0.5), (1.5, -0.5)], 0, 1, HALF, 0.15, committed, None)
    assert pf._gateway_fan_ok([(-1.5, -0.8), (1.5, -0.8)], 0, 1, HALF, 0.15, committed, None)
    open_pf = _pf([_pad(0.0, 0.0)], edge=None)
    assert open_pf._gateway_fan_ok(
        [(-1.5, -0.5), (1.5, -0.5)], 0, 1, HALF, 0.15, open_pf._fresh_committed(), None
    )


# ---------------------------------------------------------------------------
# Coupled finish gate, end to end: the emitted legs clear their own pads
# ---------------------------------------------------------------------------

_HS = NetClassRouting(
    name="HS",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.1,
    coupled_routing=True,
)
_PITCH = _HS.trace_width + _HS.effective_intra_pair_clearance()


def _coupled_pf(edge: float | None) -> LatticePathfinder:
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]
    p_a = _pad(3.0, 5.0, 1, "D+", ref="J1")
    n_a = _pad(3.0, 6.0, 2, "D-", ref="J1")
    p_b = _pad(20.0, 15.0, 1, "D+", ref="U1")
    n_b = _pad(21.0, 15.0, 2, "D-", ref="U1")
    return LatticePathfinder(outline, [p_a, n_a, p_b, n_b], _rules(edge), LayerStack.two_layer())


def _coupled_connection(pf: LatticePathfinder) -> CoupledConnection:
    pads = pf.pads
    return CoupledConnection(
        key=("pair", 1, 2),
        pair_name="D",
        pad_p_a=pads[0],
        pad_n_a=pads[1],
        pad_p_b=pads[2],
        pad_n_b=pads[3],
        net_class=_HS,
        pitch=_PITCH,
    )


def _layer_index(pf: LatticePathfinder, layer: Layer) -> int:
    return {pf.layer_stack.index_to_layer_enum(ld.index): ld.index for ld in pf.layer_stack.layers}[
        layer
    ]


def test_coupled_pair_emits_no_in_band_leg_and_still_couples() -> None:
    for edge in (None, EDGE):
        pf = _coupled_pf(edge)
        pc = _coupled_connection(pf)
        routes, _stats = pf.route_netset([], coupled=[pc])
        assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_outcomes
        for (_key, _side), route in routes.items():
            for seg in route.segments:
                li = _layer_index(pf, seg.layer)
                assert not pf.obstacles.same_net_pad_band_blocked(
                    (seg.x1, seg.y1), (seg.x2, seg.y2), li, seg.net, seg.width / 2.0, EDGE
                ), (seg, edge)
