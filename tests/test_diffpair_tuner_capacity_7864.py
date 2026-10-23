"""1x skew-tuner capacity on board 06rf ``USB3_RX1`` (issue #7864).

Operator ruling (2026-10-23): find out whether the gated skew tuner can fit
the bumps ``USB3_RX1`` needs at a **1x** excursion cap, within the bounds
(``MAX_EXCURSION_GAPS``, ``EXCURSION_HEADROOM``, the Q16 jog and proximity
gates, the Q32 chamfers, every clearance).  It cannot, and these tests pin
the measured reason.

The fixture is the pair's real pre-tune copper from the 06rf lattice route
(seed 42, ``c162396a8``), captured at the tuner's entry, plus the
neighbouring ``USB3_TX1-`` leg.  Measured there:

* At 1x the bump amplitude is ``0.99 * 0.1016 = 0.1006`` mm.  A loop then
  adds 0.083 mm (0.55- and 0.25-width chamfers, both clamped to half the
  amplitude) or 0.105 mm (0.15 widths), and takes 1.82-1.85 mm of host: two
  3W jogs (W = 0.275 mm) plus four chamfer advances.
* The generator fits ``int((0.9 L - jog) / loop_forward)`` loops on a host of
  length ``L``.  Only four of ``USB3_RX1+``'s 19 segments hold any loop:
  22.4 mm (10 loops), 11.794 mm (5), 10.182 mm (4) and 3.128 mm (1).  The
  20 loops add at most 2.09 mm.  The pair needs 3.18 mm (3.68 mm skew,
  0.5 mm limit), so ``USB3_RX1`` stays above 1.5 mm even with every
  clearance check switched off.
* Clearance takes four more loops away.  On the 11.794 mm body host the
  inner side is the partner at the 0.1016 mm gap.  The outer side has
  ``USB3_TX1-`` 0.174 mm away (edge to edge) for x = 109.5-117.0, under the
  0.15 mm USB3 class clearance once a bump rises at all.  That host takes
  one loop, at its far end.

At the shipped 2x the same copper tunes to 0.45 mm, so the 1x result comes
from the bounds, not from a tuner fault.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.core.geometry import segment_clearance
from kicad_tools.router import diffpair_bump_gates as gates
from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairType,
    DifferentialSignal,
)
from kicad_tools.router.diffpair_bump_gates import (
    EXCURSION_HEADROOM,
    MAX_EXCURSION_GAPS,
    jog_spaced,
)
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length_tuning import (
    SKEW_BUMP_CHAMFER_LADDER,
    SKEW_BUMP_CHAMFER_WIDTHS,
    SKEW_BUMP_FOOT_CHAMFER_MM,
    _outer_normal_hint,
    tune_diff_pair_skew,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.optimizer.serpentine import SerpentineConfig, SerpentineGenerator
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.skew_ledger import build_skew_ledger

W = 0.275  # USB3 trace width on 06rf
GAP = 0.1016  # the pair's routed edge-to-edge gap (intra-pair clearance)
USB3_CLEARANCE = 0.15  # the USB3 net class clearance to foreign nets
TOLERANCE = 0.5
P_ID, N_ID, TX1N_ID = 10, 11, 9

F, B = Layer.F_CU, Layer.B_CU

# Pre-tune copper, 06rf lattice seed 42 (mm).  Gateway emission order:
# fan A, fan B, then the coupled body.
_P_SEGS = [
    (109.0, 57.5, 108.9343, 57.5, F),
    (108.9343, 57.5, 108.0858, 58.3485, F),
    (154.77, 64.96, 154.2043, 64.3943, F),
    (108.0858, 58.3485, 107.5668, 58.8675, B),
    (107.5668, 58.8675, 107.5668, 58.9668, B),
    (107.5668, 58.9668, 107.3, 59.2336, B),
    (107.3, 59.2336, 105.0884, 57.022, B),
    (105.0884, 57.022, 105.0884, 55.978, B),
    (105.0884, 55.978, 106.178, 54.8884, B),
    (106.178, 54.8884, 106.578, 54.8884, B),
    (106.578, 54.8884, 108.203, 53.2634, B),
    (108.203, 53.2634, 119.997, 53.2634, B),
    (119.997, 53.2634, 120.822, 54.0884, B),
    (120.822, 54.0884, 143.222, 54.0884, B),
    (143.222, 54.0884, 150.422, 61.2884, B),
    (150.422, 61.2884, 152.822, 61.2884, B),
    (152.822, 61.2884, 153.9116, 62.378, B),
    (153.9116, 62.378, 153.9116, 64.1016, B),
    (153.9116, 64.1016, 154.2043, 64.3943, B),
]
_N_SEGS = [
    (110.0, 57.5, 110.0, 57.5657, F),
    (110.0, 57.5657, 108.6515, 58.9142, F),
    (156.04, 64.96, 155.4743, 64.3943, F),
    (108.6515, 58.9142, 108.3325, 59.2332, B),
    (108.3325, 59.2332, 107.8332, 59.2332, B),
    (107.8332, 59.2332, 107.3, 59.7664, B),
    (107.3, 59.7664, 104.7116, 57.178, B),
    (104.7116, 57.178, 104.7116, 55.822, B),
    (104.7116, 55.822, 106.022, 54.5116, B),
    (106.022, 54.5116, 106.422, 54.5116, B),
    (106.422, 54.5116, 108.047, 52.8866, B),
    (108.047, 52.8866, 120.153, 52.8866, B),
    (120.153, 52.8866, 120.978, 53.7116, B),
    (120.978, 53.7116, 143.378, 53.7116, B),
    (143.378, 53.7116, 150.578, 60.9116, B),
    (150.578, 60.9116, 152.978, 60.9116, B),
    (152.978, 60.9116, 154.2884, 62.222, B),
    (154.2884, 62.222, 154.2884, 63.5, B),
    (154.2884, 63.5, 154.58, 63.5, B),
    (154.58, 63.5, 155.4743, 64.3943, B),
]
_TX1N_SEGS = [
    (110.0, 55.5, 110.0, 55.4343, F),
    (110.0, 55.4343, 108.6515, 54.0858, F),
    (152.23, 64.96, 151.6643, 64.3943, F),
    (108.6515, 54.0858, 109.2332, 54.6675, B),
    (109.2332, 54.6675, 109.2332, 54.7668, B),
    (109.2332, 54.7668, 109.3, 54.8336, B),
    (109.3, 54.8336, 109.5116, 54.622, B),
    (109.5116, 54.622, 109.5116, 54.222, B),
    (109.5116, 54.222, 110.022, 53.7116, B),
    (110.022, 53.7116, 116.578, 53.7116, B),
    (116.578, 53.7116, 116.978, 54.1116, B),
    (116.978, 54.1116, 119.778, 54.1116, B),
    (119.778, 54.1116, 120.928, 55.2616, B),
    (120.928, 55.2616, 121.728, 55.2616, B),
    (121.728, 55.2616, 123.378, 56.9116, B),
    (123.378, 56.9116, 143.378, 56.9116, B),
    (143.378, 56.9116, 146.578, 60.1116, B),
    (146.578, 60.1116, 148.178, 60.1116, B),
    (148.178, 60.1116, 150.2884, 62.222, B),
    (150.2884, 62.222, 150.2884, 63.5, B),
    (150.2884, 63.5, 150.77, 63.5, B),
    (150.77, 63.5, 151.6643, 64.3943, B),
]
_VIAS = {
    P_ID: [(108.0858, 58.3485), (154.2043, 64.3943)],
    N_ID: [(108.6515, 58.9142), (155.4743, 64.3943)],
    TX1N_ID: [(108.6515, 54.0858), (151.6643, 64.3943)],
}
_NAMES = {P_ID: "USB3_RX1+", N_ID: "USB3_RX1-", TX1N_ID: "USB3_TX1-"}

#: The body host whose outer side ``USB3_TX1-`` crowds.
BODY_HOST = (108.203, 53.2634, 119.997, 53.2634)


def _route(net: int, segs) -> Route:
    name = _NAMES[net]
    return Route(
        net=net,
        net_name=name,
        segments=[
            Segment(x1, y1, x2, y2, W, layer, net=net, net_name=name)
            for x1, y1, x2, y2, layer in segs
        ],
        vias=[Via(x, y, 0.3, 0.6, (F, B), net=net, net_name=name) for x, y in _VIAS[net]],
    )


def _routes() -> dict[int, Route]:
    return {
        P_ID: _route(P_ID, _P_SEGS),
        N_ID: _route(N_ID, _N_SEGS),
        TX1N_ID: _route(TX1N_ID, _TX1N_SEGS),
    }


def _pair() -> DetectedPair:
    def sig(name: str, net_id: int, pol: str) -> DifferentialSignal:
        return DifferentialSignal(
            net_name=name, net_id=net_id, base_name="USB3_RX1", polarity=pol, notation="explicit"
        )

    return DetectedPair(
        pair=DifferentialPair(
            name="USB3_RX1",
            positive=sig("USB3_RX1+", P_ID, "P"),
            negative=sig("USB3_RX1-", N_ID, "N"),
            pair_type=DifferentialPairType.USB3,
        ),
        source=DetectionSource.EXPLICIT,
    )


def _length(route: Route) -> float:
    return LengthTracker.calculate_route_length(route)


def _need() -> float:
    routes = _routes()
    return abs(_length(routes[N_ID]) - _length(routes[P_ID])) - TOLERANCE


def _one_x_amplitude() -> float:
    return EXCURSION_HEADROOM * 1.0 * GAP


def _rungs() -> list[tuple[float, float | None]]:
    """Every plain-bump chamfer the gated tuner tries (Q32: never square)."""
    return [(c, None) for c in SKEW_BUMP_CHAMFER_LADDER] + [
        (SKEW_BUMP_CHAMFER_WIDTHS, SKEW_BUMP_FOOT_CHAMFER_MM)
    ]


def _clearance(a: Segment, b: Segment) -> float:
    return segment_clearance(a.x1, a.y1, a.x2, a.y2, a.width, b.x1, b.y1, b.x2, b.y2, b.width)


def _usb3_class_gate(routes: dict[int, Route]):
    """The lattice path's foreign-copper gate, for segments (Issue #6664).

    The board run passes ``Autorouter._skew_tune_foreign_clearance_ok``,
    which holds a bump to the 0.15 mm USB3 class clearance from foreign
    copper (the partner stays at the gap).  This is its segment branch.
    """

    def ok(new_segments, net, host_segments=None) -> bool:
        partner = N_ID if net == P_ID else P_ID
        for seg in new_segments:
            for other_net, route in routes.items():
                if other_net in (net, partner):
                    continue
                for o in route.segments:
                    if o.layer == seg.layer and _clearance(seg, o) + 1e-9 < USB3_CLEARANCE:
                        return False
        return True

    return ok


def _tune(**kwargs):
    routes = _routes()
    p, n, result = tune_diff_pair_skew(
        _pair(),
        routes,
        tolerance_mm=TOLERANCE,
        intra_pair_clearance_mm=GAP,
        ledger=build_skew_ledger(routes[P_ID], routes[N_ID]),
        extra_clearance_ok=_usb3_class_gate(routes),
        **kwargs,
    )
    return routes, p, n, result


def _bump_runs(original: Route, tuned: Route) -> list[list[Segment]]:
    """Maximal runs of segments the tuner inserted (bumps plus their leads)."""
    kept = {id(s) for s in original.segments}
    runs: list[list[Segment]] = []
    current: list[Segment] = []
    for seg in tuned.segments:
        if id(seg) in kept:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(seg)
    if current:
        runs.append(current)
    return runs


def _point_to_segment(x: float, y: float, s: Segment) -> float:
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    t = max(0.0, min(1.0, ((x - s.x1) * dx + (y - s.y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (s.x1 + t * dx), y - (s.y1 + t * dy))


def _turn_deg(a: Segment, b: Segment) -> float:
    ax, ay = a.x2 - a.x1, a.y2 - a.y1
    bx, by = b.x2 - b.x1, b.y2 - b.y1
    cos = (ax * bx + ay * by) / (math.hypot(ax, ay) * math.hypot(bx, by))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def test_fixture_is_the_measured_pair() -> None:
    routes = _routes()
    assert _length(routes[N_ID]) - _length(routes[P_ID]) == pytest.approx(3.680, abs=1e-3)
    assert _need() == pytest.approx(3.180, abs=1e-3)
    # The bound under study: shipped at 2x, measured here at 1x.
    assert MAX_EXCURSION_GAPS == 2.0


@pytest.mark.parametrize(("chamfer", "foot"), _rungs())
def test_one_x_generator_capacity_is_below_the_need(chamfer: float, foot: float | None) -> None:
    """Every host the generator can use, at its fullest, still leaves the pair failing.

    No clearance, proximity or uncoupled check is applied here: this is the
    most the tuner's bump model can add to ``USB3_RX1+`` at 1x.
    """
    amp = _one_x_amplitude()
    route = _routes()[P_ID]
    partner = _routes()[N_ID]
    config = jog_spaced(
        SerpentineConfig(
            amplitude=amp,
            min_segment_length=0.0,
            corner_chamfer_widths=chamfer,
            foot_chamfer_length_mm=foot,
        ),
        W,
    )
    added = 0.0
    loops_by_host: dict[float, int] = {}
    for seg in route.segments:
        cfg = SerpentineConfig(
            amplitude=amp,
            min_spacing=config.min_spacing,
            min_segment_length=0.0,
            gap_factor=config.gap_factor,
            side="outer",
            outer_normal_hint=_outer_normal_hint(seg, partner),
            corner_chamfer_widths=chamfer,
            foot_chamfer_length_mm=foot,
        )
        serp = SerpentineGenerator(cfg).generate_trombone(seg, 100.0)
        if serp.success and serp.num_loops:
            loops_by_host[round(math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1), 3)] = serp.num_loops
            added += serp.length_added
    assert loops_by_host == {22.4: 10, 11.794: 5, 10.182: 4, 3.128: 1}
    # 20 loops at 0.083 mm (0.105 mm at 0.15-width chamfers) per loop.
    assert added == pytest.approx(20 * config.loop_length_added(W, amp), abs=1e-6)
    assert added < 2.1 < _need()


def test_one_x_body_host_is_boxed_in_on_both_sides() -> None:
    """Inner side: the partner at the gap.  Outer side: ``USB3_TX1-``.

    A crown at the 1x amplitude anywhere over x = 110.0-116.6 sits under the
    0.15 mm USB3 clearance to ``USB3_TX1-`` (and under the 0.1016 mm floor
    of the tuner's own check); toward the partner it would close the gap.
    """
    routes = _routes()
    amp = _one_x_amplitude()
    x1, y1, x2, y2 = BODY_HOST
    host = Segment(x1, y1, x2, y2, W, B)
    tx1n = [s for s in routes[TX1N_ID].segments if s.layer == B]
    partner = [s for s in routes[N_ID].segments if s.layer == B]

    def clearance(segs: list[Segment], y: float, x: float) -> float:
        return min(
            segment_clearance(x - 0.4, y, x + 0.4, y, W, s.x1, s.y1, s.x2, s.y2, s.width)
            for s in segs
        )

    # The flat host itself: 0.174 mm to USB3_TX1-, the gap to the partner.
    assert clearance(tx1n, host.y1, 113.0) == pytest.approx(0.174, abs=1e-3)
    assert clearance(partner, host.y1, 113.0) == pytest.approx(GAP, abs=1e-3)
    for x in (110.5, 112.0, 113.5, 115.0, 116.2):
        assert clearance(tx1n, host.y1 + amp, x) < GAP < USB3_CLEARANCE
        assert clearance(partner, host.y1 - amp, x) < 0.01
    # Past USB3_TX1-'s jog the outer side is free: where the one loop lands.
    assert clearance(tx1n, host.y1 + amp, 118.5) > USB3_CLEARANCE


def test_one_x_tuner_stops_above_the_limit_with_gated_chamfered_bumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 1x the tuner commits what fits and stops, every bump within bounds."""
    monkeypatch.setattr(gates, "MAX_EXCURSION_GAPS", 1.0)
    routes, p, n, result = _tune()
    assert not result.success
    # Measured on the board run too (06rf lattice seed 42): five inserts,
    # 3.68 -> 2.27 mm, then every placement fails a clearance check.
    assert result.reason == "post_insertion_drc_violation"
    assert result.inserts_applied == 5
    assert result.skew_before_mm == pytest.approx(3.680, abs=1e-3)
    assert result.skew_after_mm == pytest.approx(2.271, abs=5e-3)
    # Only the shorter half (P) was bumped; the longer comes back by reference.
    assert n is routes[N_ID] and n.segments is routes[N_ID].segments
    runs = _bump_runs(routes[P_ID], p)
    assert runs
    original = routes[P_ID].segments
    bends = {(round(s.x2, 4), round(s.y2, 4)) for s in original}

    def off_route(x: float, y: float) -> float:
        return min(_point_to_segment(x, y, s) for s in original)

    for run in runs:
        # Excursion: within 1x the gap of the untuned route.
        excursion = max(off_route(x, y) for s in run for x, y in ((s.x1, s.y1), (s.x2, s.y2)))
        assert excursion <= 1.0 * GAP + 1e-6
        # Q32: every corner of a bump is a 45-degree chamfer, feet included
        # (the route's own bends, where two bumped hosts meet, excepted).
        for a, b in zip(run, run[1:], strict=False):
            if (round(a.x2, 4), round(a.y2, 4)) in bends:
                continue
            turn = _turn_deg(a, b)
            assert turn == pytest.approx(0.0, abs=1e-6) or turn == pytest.approx(45.0, abs=1e-3)
        # Clearance: the gap to the partner, the USB3 clearance to USB3_TX1-.
        for seg in run:
            for other, floor in ((routes[N_ID], GAP), (routes[TX1N_ID], USB3_CLEARANCE)):
                for o in other.segments:
                    if o.layer != seg.layer:
                        continue
                    assert _clearance(seg, o) + 1e-9 >= floor
    # The body host took one loop, past USB3_TX1-'s jog away from it.
    x1, y1, x2, _ = BODY_HOST
    raised = [
        s
        for run in runs
        for s in run
        if x1 <= min(s.x1, s.x2) and max(s.x1, s.x2) <= x2 and max(s.y1, s.y2) > y1 + 1e-6
    ]
    assert raised
    assert all(min(s.x1, s.x2) > 116.978 for s in raised)


def test_two_x_same_copper_tunes_inside_the_limit() -> None:
    """At the shipped 2x the tuner closes the same pair: the 1x gap is the bound."""
    routes, p, n, result = _tune()
    assert result.success, result.message
    assert result.skew_after_mm <= TOLERANCE
    assert n is routes[N_ID]


def test_no_legal_host_rolls_back_by_reference() -> None:
    """The rollback contract: no legal placement returns both routes unchanged."""

    def seg(x1, y1, x2, y2, net, name):
        return Segment(x1, y1, x2, y2, W, B, net=net, net_name=name)

    pitch = W + GAP
    p = Route(net=1, net_name="D+", segments=[seg(0, 0, 20, 0, 1, "D+")])
    n = Route(net=2, net_name="D-", segments=[seg(0, -pitch, 22, -pitch, 2, "D-")])
    # A foreign trace at the gap above D+: no bump fits on either side.
    x = Route(net=3, net_name="X", segments=[seg(-1, pitch, 23, pitch, 3, "X")])
    routes = {1: p, 2: n, 3: x}

    def sig(name: str, net_id: int, pol: str) -> DifferentialSignal:
        return DifferentialSignal(
            net_name=name, net_id=net_id, base_name="D", polarity=pol, notation="plus_minus"
        )

    pair = DetectedPair(
        pair=DifferentialPair(
            name="D",
            positive=sig("D+", 1, "P"),
            negative=sig("D-", 2, "N"),
            pair_type=DifferentialPairType.USB3,
        ),
        source=DetectionSource.EXPLICIT,
    )
    p_out, n_out, result = tune_diff_pair_skew(
        pair, routes, tolerance_mm=TOLERANCE, intra_pair_clearance_mm=GAP
    )
    assert not result.success
    assert result.reason == "post_insertion_drc_violation"
    assert result.inserts_applied == 0
    assert p_out is p and p_out.segments is p.segments
    assert n_out is n and n_out.segments is n.segments
