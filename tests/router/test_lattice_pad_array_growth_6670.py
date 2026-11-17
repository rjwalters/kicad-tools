"""Pair legs past a pad array keep the pad's class floor (issue #6670).

On board 06rf the lattice routed every connection, but the uncoupled-fallback
legs of ``USB3_TX2`` / ``USB3_RX2`` passed 0.168 / 0.178 mm from U2's GND
balls (``Power``, 0.200 mm).  The #6395 repair never moves pair legs, and the
#6489 grown-keep-out re-negotiation chased the shortfall from ball to ball:
two rounds cut 9 shortfalls to 7 to 2, the round budget ran out, and the
authored-floor backstop deleted both nets.

The fix grows a part's whole class-floored pad set once a shortfall lands on a
part whose pads were already grown, so the search sees the array as one
constraint.  The backstop log also names the pad (``GND pad U2.G3``) instead of
the neutralised ``net 0 pad``.
"""

from __future__ import annotations

from kicad_tools.router.authored_clearance import authored_violations, pad_label
from kicad_tools.router.clearance_shapes import copper_gap, pad_shape, segment_shape
from kicad_tools.router.lattice import pathfinder as pathfinder_mod
from kicad_tools.router.lattice.class_pad_repair import ClassPadRepairStats
from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import LatticeNegotiationStats, LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

BASE = 0.15  # board default, and the USB3 class's own clearance
POWER = 0.2  # the GND pour net's router class
EPS = 1e-6
PITCH = 1.27
BALL = 0.45


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2, trace_clearance=BASE, via_clearance=BASE, grid_resolution=0.05
    )


def _ball(r: int, c: int, net: int, name: str) -> Pad:
    x, y = 10.0 + c * PITCH, 7.0 + r * PITCH
    pin = f"{'ABCDEF'[r]}{c + 1}"
    return Pad(x=x, y=y, width=BALL, height=BALL, net=net, net_name=name, ref="U2", pin=pin)


def _bga_pair() -> tuple[LatticePathfinder, CoupledConnection, list[Pad]]:
    """A 6x6 1.27 mm BGA of neutralised GND balls with a USB3 pair at C3/C4.

    The pair cannot couple out of the ball field, so both legs route
    single-ended (``uncoupled-fallback``) between GND balls -- the 06rf case.
    """
    pads: list[Pad] = []
    gnd: list[Pad] = []
    pp_b = pn_b = None
    for r in range(6):
        for c in range(6):
            if (r, c) == (2, 2):
                pp_b = _ball(r, c, 1, "USB3_TX+")
                pads.append(pp_b)
            elif (r, c) == (2, 3):
                pn_b = _ball(r, c, 2, "USB3_TX-")
                pads.append(pn_b)
            else:
                ball = _ball(r, c, 0, "GND")  # skipped pour net: net 0 + name
                pads.append(ball)
                gnd.append(ball)
    assert pp_b is not None and pn_b is not None
    pp_a = Pad(x=2.0, y=9.5, width=0.6, height=0.6, net=1, net_name="USB3_TX+", ref="J2", pin="1")
    pn_a = Pad(x=2.0, y=10.5, width=0.6, height=0.6, net=2, net_name="USB3_TX-", ref="J2", pin="2")
    pads += [pp_a, pn_a]
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    pf = LatticePathfinder(outline, pads, _rules(), LayerStack.two_layer())
    pf.set_pad_class_clearances([POWER if p.net == 0 else BASE for p in pads])
    pc = CoupledConnection(
        key=("pair", 1, 2),
        pair_name="USB3_TX",
        pad_p_a=pp_a,
        pad_n_a=pn_a,
        pad_p_b=pp_b,
        pad_n_b=pn_b,
        net_class=NetClassRouting(name="USB3", trace_width=0.275, clearance=BASE),
        pitch=1.0,
    )
    return pf, pc, gnd


def _min_gap(routes: dict, pads: list[Pad]) -> float:
    return min(
        (
            copper_gap(segment_shape(seg), pad_shape(pad))
            for route in routes.values()
            for seg in route.segments
            for pad in pads
            if seg.layer == pad.layer
        ),
        default=float("inf"),
    )


def _partner_blind_fallback(monkeypatch) -> None:
    """Route fallback legs the pre-#6703 way (P, then N, as unrelated nets).

    Issue #6703 searches partner-aware fallback legs at the strictest pad
    class, so on this fixture they keep the floor without any growth round.
    The growth mechanism under test is still the only remedy for partner-blind
    legs, so this test routes them that way.
    """

    def blind(self, pc, **kw):
        return self._partner_blind_legs(pc, **kw), True

    monkeypatch.setattr(LatticePathfinder, "_partner_aware_legs", blind)


def test_fallback_legs_through_a_gnd_ball_field_keep_the_power_floor(monkeypatch) -> None:
    """The 06rf mechanism, small: legs routed past neutralised GND balls.

    Before the fix the second growth round grew only the newly hit balls and
    the leg landed next to yet another one, leaving copper the backstop
    would delete.  Now the whole part grows and the legs keep 0.2 mm.
    """
    _partner_blind_fallback(monkeypatch)
    pf, pc, gnd = _bga_pair()
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    assert (pc.key, "P") in routes and (pc.key, "N") in routes
    assert stats.routed == stats.total
    assert _min_gap(routes, gnd) >= POWER - EPS
    assert pf.class_pad_repair_stats is not None
    assert pf.class_pad_repair_stats.total_remaining == 0
    rounds = pf.class_pad_growth_rounds
    assert rounds and all(r.kept for r in rounds)
    # The shortfall moved to another U2 ball, so the last round grew all of U2.
    assert rounds[-1].footprints == ("U2",)


def test_partner_aware_fallback_legs_keep_the_power_floor_without_growth() -> None:
    """Issue #6703: partner-aware legs are searched at the strictest pad class.

    The same pair keeps 0.2 mm from every GND ball on the first negotiation,
    so no grown-keep-out round (and no lattice rebuild) is needed.
    """
    pf, pc, gnd = _bga_pair()
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    assert (pc.key, "P") in routes and (pc.key, "N") in routes
    assert stats.routed == stats.total
    assert _min_gap(routes, gnd) >= POWER - EPS
    assert pf.class_pad_growth_rounds == []
    assert stats.lattice_builds == 1


def _stub_stats(routed: int) -> LatticeNegotiationStats:
    return LatticeNegotiationStats(
        iterations=1, converged=True, routed=routed, total=1, lattice_builds=1
    )


def test_growth_escalates_to_the_whole_part_only_when_the_shortfall_recurs(monkeypatch) -> None:
    """Round 1 grows only the hit pad; a recurrence on the same part grows it all.

    Each scripted negotiation leaves the given shortfall pads.  The new pad
    of a part grown before triggers the part-wide growth; base-class pads of
    that part, and pads of other parts, never grow.
    """
    pads = [
        Pad(x=5.0, y=5.0, width=0.4, height=0.4, net=0, net_name="GND", ref="U2", pin="A1"),
        Pad(x=6.0, y=5.0, width=0.4, height=0.4, net=0, net_name="GND", ref="U2", pin="A2"),
        Pad(x=7.0, y=5.0, width=0.4, height=0.4, net=0, net_name="GND", ref="U2", pin="A3"),
        Pad(x=8.0, y=5.0, width=0.4, height=0.4, net=3, net_name="SIG", ref="U2", pin="A4"),
        Pad(x=5.0, y=9.0, width=0.4, height=0.4, net=0, net_name="GND", ref="J1", pin="1"),
    ]
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    pf = LatticePathfinder(outline, pads, _rules(), LayerStack.two_layer())
    pf.set_pad_class_clearances([POWER, POWER, POWER, BASE, POWER])
    left_by_call = [{0: POWER, 4: POWER}, {1: POWER}, {}]
    grown_seen: list[dict[int, float]] = []

    def fake_once(self, connections, **kwargs):
        grown_seen.append(dict(self._class_pad_growth))
        left = left_by_call[len(grown_seen) - 1]
        self.failure_reasons, self.pair_outcomes = {}, {}
        self.class_pad_repair_stats = ClassPadRepairStats(
            unrepaired_pads=dict(left), total_remaining=len(left)
        )
        return {}, _stub_stats(1)

    monkeypatch.setattr(LatticePathfinder, "_route_netset_once", fake_once)
    _routes, _stats = pf.route_netset([])
    assert grown_seen == [{}, {0: POWER, 4: POWER}, {0: POWER, 1: POWER, 2: POWER, 4: POWER}]
    first, second = pf.class_pad_growth_rounds
    assert first.footprints == () and first.grown == 2 and first.kept
    assert second.footprints == ("U2",) and second.grown == 2 and second.kept
    assert pathfinder_mod._MAX_PAD_GROWTH_ROUNDS == 2


def test_rejected_whole_part_growth_falls_back_to_the_targeted_growth(monkeypatch) -> None:
    """A whole-part attempt that routes less is undone and the round retried targeted."""
    pads = [
        Pad(x=5.0, y=5.0, width=0.4, height=0.4, net=0, net_name="GND", ref="U2", pin="A1"),
        Pad(x=6.0, y=5.0, width=0.4, height=0.4, net=0, net_name="GND", ref="U2", pin="A2"),
        Pad(x=7.0, y=5.0, width=0.4, height=0.4, net=0, net_name="GND", ref="U2", pin="A3"),
    ]
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    pf = LatticePathfinder(outline, pads, _rules(), LayerStack.two_layer())
    pf.set_pad_class_clearances([POWER, POWER, POWER])
    # (shortfall pads, shortfall count, routed): initial, targeted round 1,
    # whole-U2 round 2 (routes less), targeted round 2.
    script = [({0: POWER}, 2, 1), ({1: POWER}, 1, 1), ({}, 0, 0), ({}, 0, 1)]
    grown_seen: list[dict[int, float]] = []

    def fake_once(self, connections, **kwargs):
        grown_seen.append(dict(self._class_pad_growth))
        left, count, routed = script[len(grown_seen) - 1]
        self.failure_reasons, self.pair_outcomes = {}, {}
        self.class_pad_repair_stats = ClassPadRepairStats(
            unrepaired_pads=dict(left), total_remaining=count
        )
        return {}, _stub_stats(routed)

    monkeypatch.setattr(LatticePathfinder, "_route_netset_once", fake_once)
    _routes, stats = pf.route_netset([])
    assert grown_seen == [
        {},
        {0: POWER},
        {0: POWER, 1: POWER, 2: POWER},
        {0: POWER, 1: POWER},
    ]
    assert stats.routed == 1
    assert [(r.footprints, r.kept) for r in pf.class_pad_growth_rounds] == [
        ((), True),
        (("U2",), False),
        ((), True),
    ]
    assert pf._class_pad_growth == {0: POWER, 1: POWER}
    assert pf.class_pad_repair_stats.total_remaining == 0


def test_rejected_growth_round_is_recorded_as_rolled_back(monkeypatch) -> None:
    pads = [Pad(x=5.0, y=5.0, width=0.4, height=0.4, net=0, net_name="GND", ref="U2", pin="A1")]
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    pf = LatticePathfinder(outline, pads, _rules(), LayerStack.two_layer())
    pf.set_pad_class_clearances([POWER])
    calls: list[int] = []

    def fake_once(self, connections, **kwargs):
        calls.append(1)
        self.failure_reasons, self.pair_outcomes = {}, {}
        self.class_pad_repair_stats = ClassPadRepairStats(
            unrepaired_pads={0: POWER}, total_remaining=1
        )
        # The grown round routes less: it must be rolled back.
        return {}, _stub_stats(1 if len(calls) == 1 else 0)

    monkeypatch.setattr(LatticePathfinder, "_route_netset_once", fake_once)
    _routes, stats = pf.route_netset([])
    assert stats.routed == 1
    (only,) = pf.class_pad_growth_rounds
    assert not only.kept and (only.routed_before, only.routed_after) == (1, 0)
    assert pf._class_pad_growth == {}


def test_floor_violation_names_the_neutralised_pads_net_and_designator() -> None:
    """``GND pad U2.G3``, not ``net 0 pad``, in the backstop's census."""
    gnd = Pad(x=0.0, y=0.0, width=0.45, height=0.45, net=0, net_name="GND", ref="U2", pin="G3")
    gnd.authored_clearance = POWER
    seg = Segment(0.5, -2.0, 0.5, 2.0, 0.275, Layer.F_CU, 13, "USB3_TX2-")
    route = Route(net=13, net_name="USB3_TX2-", segments=[seg], vias=[])
    (hit,) = authored_violations({}, [gnd], [route])
    assert hit.other_net == 0 and hit.kind == "pad"
    assert hit.other_label == "GND pad U2.G3"
    assert pad_label(Pad(x=0, y=0, width=1, height=1, net=4, net_name="")) == "net 4 pad"
