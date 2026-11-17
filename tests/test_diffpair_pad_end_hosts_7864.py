"""Pad-end skew-bump hosts on board 06rf ``USB3_RX1`` at 1x (issue #7864).

Operator ruling (2026-10-23, Q1 option 2): after #7882 showed the body hosts
top out short of the need, measure the one remaining avenue -- seating 1x
loops on the short stubs at the pad end of the pair (the J1 fan, the via
landings and the stubs beside them), within every bound.

Measured on the 06rf lattice route (seed 42, ``3b5de1f8c``, ``MAX_EXCURSION_GAPS``
patched to 1.0 locally, never committed), with the board's own foreign-copper
gate (``Autorouter._skew_tune_foreign_clearance_ok``: pads, vias, segments)
and the board's own length model (``KicadLengthModel``, kicad-cli exact):

* The board's pre-tune ``USB3_RX1+`` is the #7882 fixture segment for
  segment, and **no** segment is escape copper (``fixed_segment_ids`` is
  empty for it), so escape exclusion is not why the stubs are unused.
* Every pad-end stub is refused today by ``min_segment_length`` (2.0 mm) and,
  with that lifted, by the generator's ``int((0.9 L - jog) / loop_forward)``
  (0 loops).  A single 1x loop seated flush (zero-to-0.02 mm leads) needs only
  0.990 mm of host at the 0.15 W rung, so one loop does fit on each.
* Stub 1 (the J1 fan, F.Cu): its outer side passes 0.104 mm from J1.B12
  (GND, ``Power`` class, 0.2 mm).  Its inner side is legal (the fan's partner
  leg is 0.5 mm away), but that loop sits between the J1.B11 pad and the J1
  via, whose copper kicad-cli's length measure straightens: it adds at most
  0.0697 mm of measured length, not 0.1045 mm.
* Stub 7: 0.1735 mm from the J1.S1 shield pad (GND through-hole, 0.2 mm) at
  every anchor on its outer side; the partner at the gap on its inner side.
* Stubs 8, 16, 17: one loop each, outer side; two loops do not fit.
* The via landings (index 2, the U2 fan; 3; 18) are shorter than one loop.

Summed best case (every host, loops seated flush, every gate, measured the
way ``kct check`` measures): 29 loops, +2.997 mm, 1x skew **0.680 mm** --
over 0.5 mm (``main``) and over the 0.58 mm #7883 proposes.  What binds is
not the pad end: the 11.794 mm body host takes 2 loops where its length
holds 6, because ``USB3_TX1-`` crowds its outer side (#7882; source-side,
out of scope here).  With those 4 loops the same model reaches about 0.26 mm.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.core.geometry import point_to_segment_distance, segment_clearance
from kicad_tools.router import diffpair_bump_gates as gates
from kicad_tools.router.diffpair_bump_gates import EXCURSION_HEADROOM, MIN_JOG_WIDTHS, jog_spaced
from kicad_tools.router.diffpair_length_tuning import (
    SKEW_BUMP_FOOT_CHAMFER_MM,
    _outer_normal_hint,
)
from kicad_tools.router.grid import pad_segment_edge_clearance
from kicad_tools.router.kicad_length import KicadLengthModel, KicadStackup
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.serpentine import SerpentineConfig, SerpentineGenerator
from kicad_tools.router.primitives import Pad, Route, Segment
from tests.test_diffpair_tuner_capacity_7864 import (
    GAP,
    N_ID,
    P_ID,
    TOLERANCE,
    TX1N_ID,
    USB3_CLEARANCE,
    W,
    _need,
    _routes,
    _usb3_class_gate,
)

#: GND (and every other ``Power`` net) on 06rf: the class clearance the board
#: gate holds a bump to near J1.B12 and J1.S1.
POWER_CLEARANCE = 0.2
#: #7883 / PR #7886's proposed phase-derived USB3 limit (-30 dB), pending.
PROPOSED_LIMIT = 0.58
#: The rung that adds the most per loop at 1x (0.15 W crown and foot).
RUNG = 0.15

#: Pad-end stubs of ``USB3_RX1+`` (index in the fixture: where it sits).
PAD_END_STUBS = {
    1: "J1 pad B11 -> J1 via (F.Cu fan)",
    7: "J1 side, beside J1.S1",
    8: "J1 side, beside J1.S1",
    16: "U2 side",
    17: "U2 via landing side",
}
#: Via-landing stubs too short for any loop.
LANDING_STUBS = (2, 3, 18)

# Board pads by the pair's ends (06rf, ``3b5de1f8c``).  GND is a plane net.
_J1_B11 = Pad(109.0, 57.5, 0.3, 0.6, P_ID, "USB3_RX1+", Layer.F_CU, ref="J1", pin="B11")
_J1_B10 = Pad(110.0, 57.5, 0.3, 0.6, N_ID, "USB3_RX1-", Layer.F_CU, ref="J1", pin="B10")
_J1_B12 = Pad(108.0, 57.5, 0.3, 0.6, 0, "GND", Layer.F_CU, ref="J1", pin="B12")
_J1_S1 = Pad(
    106.0,
    56.5,
    1.0,
    1.0,
    0,
    "GND",
    Layer.F_CU,
    ref="J1",
    pin="S1",
    through_hole=True,
    drill=0.6,
    shape="circle",
)
_U2_B5 = Pad(154.77, 64.96, 0.45, 0.45, P_ID, "USB3_RX1+", Layer.F_CU, ref="U2", pin="B5")
FOREIGN_PADS = (_J1_B12, _J1_S1)


def _amp() -> float:
    return EXCURSION_HEADROOM * 1.0 * GAP


def _config(chamfer: float = RUNG) -> SerpentineConfig:
    return jog_spaced(
        SerpentineConfig(amplitude=_amp(), min_segment_length=0.0, corner_chamfer_widths=chamfer),
        W,
    )


def _jog(cfg: SerpentineConfig) -> float:
    return cfg.min_spacing * cfg.gap_factor


def _footprint(loops: int, cfg: SerpentineConfig) -> float:
    """Host one flush-seated bump of ``loops`` loops occupies (no leads)."""
    return loops * (_jog(cfg) + 2 * cfg.leg_advance_mm(W, _amp())) + (loops - 1) * _jog(cfg)


def _length(seg: Segment) -> float:
    return math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)


def _flush_bump(host: Segment, side: str, t: float, loops: int = 1) -> list[Segment]:
    """``host`` with a chamfered 1x bump starting ``t`` mm along it.

    The shipped generator always leads in by one jog and keeps a 10% margin;
    this seats the bump anywhere, flush against either end included.
    """
    cfg = _config()
    amp = _amp()
    crown = cfg.chamfer_mm(W, amp)
    foot = cfg.foot_chamfer_mm(W, amp)
    jog = _jog(cfg)
    length = _length(host)
    ux, uy = (host.x2 - host.x1) / length, (host.y2 - host.y1) / length
    hint = _outer_normal_hint(host, _routes()[N_ID])
    sign = 1.0 if (-uy * hint[0] + ux * hint[1]) >= 0 else -1.0
    if side == "inner":
        sign = -sign
    nx, ny = -uy * sign, ux * sign
    x, y = host.x1 + ux * t, host.y1 + uy * t
    pts = [(host.x1, host.y1)] + ([(x, y)] if t > 1e-9 else [])

    def step(along: float, up: float) -> None:
        nonlocal x, y
        x, y = x + ux * along + nx * up, y + uy * along + ny * up
        pts.append((x, y))

    rise = amp - foot - crown
    for i in range(loops):
        step(foot, foot)
        step(0.0, rise)
        step(crown, crown)
        step(jog, 0.0)
        step(crown, -crown)
        step(0.0, -rise)
        step(foot, -foot)
        if i < loops - 1:
            step(jog, 0.0)
    if math.hypot(host.x2 - x, host.y2 - y) > 1e-9:
        pts.append((host.x2, host.y2))
    return [
        Segment(a[0], a[1], b[0], b[1], W, host.layer, net=host.net, net_name=host.net_name)
        for a, b in zip(pts, pts[1:], strict=False)
    ]


def _anchors(host: Segment, loops: int = 1, step: float = 0.01) -> list[float]:
    span = _length(host) - _footprint(loops, _config())
    return sorted({0.0, span, *(i * step for i in range(int(span / step) + 1))})


def _pad_clearance(bump: list[Segment], pad: Pad) -> float:
    return min(
        (
            pad_segment_edge_clearance(pad, s.x1, s.y1, s.x2, s.y2, s.width / 2)
            for s in bump
            if pad.through_hole or s.layer == pad.layer
        ),
        default=math.inf,
    )


def _route_clearance(bump: list[Segment], route: Route) -> float:
    gaps = [
        segment_clearance(s.x1, s.y1, s.x2, s.y2, s.width, o.x1, o.y1, o.x2, o.y2, o.width)
        for s in bump
        for o in route.segments
        if o.layer == s.layer
    ]
    gaps += [
        point_to_segment_distance(v.x, v.y, s.x1, s.y1, s.x2, s.y2) - s.width / 2 - v.diameter / 2
        for s in bump
        for v in route.vias
    ]
    return min(gaps)


def _partner_clearance(bump: list[Segment]) -> float:
    return min(_route_clearance(bump, _routes()[N_ID]), _pad_clearance(bump, _J1_B10))


def _turn(a: Segment, b: Segment) -> float:
    ax, ay, bx, by = a.x2 - a.x1, a.y2 - a.y1, b.x2 - b.x1, b.y2 - b.y1
    cos = (ax * bx + ay * by) / (math.hypot(ax, ay) * math.hypot(bx, by))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def _measured_length(route: Route) -> float:
    """``USB3_RX1+`` the way ``kct check`` / kicad-cli measure it (pads clipped)."""
    model = KicadLengthModel.for_router(KicadStackup.default(1.6, 2), [_J1_B11, _U2_B5])
    return model.route_length(route)


def _with_bump(idx: int, bump: list[Segment]) -> Route:
    p = _routes()[P_ID]
    return Route(
        net=p.net,
        net_name=p.net_name,
        segments=p.segments[:idx] + bump + p.segments[idx + 1 :],
        vias=p.vias,
    )


@pytest.mark.parametrize("idx", sorted(PAD_END_STUBS))
def test_pad_end_stub_is_not_a_tuner_host_today(idx: int) -> None:
    """``min_segment_length`` refuses it; lifted, the generator fits 0 loops."""
    host = _routes()[P_ID].segments[idx]
    assert _length(host) < SerpentineConfig().min_segment_length
    cfg = _config()
    assert int((0.9 * _length(host) - _jog(cfg)) / cfg.loop_forward(W, _amp())) == 0
    refused = SerpentineGenerator(_config()).generate_trombone(host, 1.0)
    assert not refused.success
    assert "too short" in refused.message


@pytest.mark.parametrize("idx", sorted(PAD_END_STUBS))
def test_one_flush_loop_fits_each_pad_end_stub_two_do_not(idx: int) -> None:
    host = _routes()[P_ID].segments[idx]
    cfg = _config()
    assert _footprint(1, cfg) == pytest.approx(0.990, abs=1e-3)
    assert _footprint(1, cfg) <= _length(host) < _footprint(2, cfg)


@pytest.mark.parametrize("idx", LANDING_STUBS)
def test_via_landing_stubs_are_shorter_than_one_loop(idx: int) -> None:
    host = _routes()[P_ID].segments[idx]
    assert _length(host) < _footprint(1, _config())


def test_stub_1_outer_side_is_blocked_by_j1_b12() -> None:
    """Every outer anchor on the J1 fan stub is under GND's 0.2 mm to J1.B12."""
    host = _routes()[P_ID].segments[1]
    best = max(_pad_clearance(_flush_bump(host, "outer", t), _J1_B12) for t in _anchors(host))
    assert best == pytest.approx(0.1044, abs=1e-3)
    assert best < POWER_CLEARANCE


def test_stub_1_inner_loop_is_legal_but_the_pads_eat_its_length() -> None:
    """The inner side clears everything, but sits in J1.B11's and the via's copper.

    kicad-cli measures a track chain from the pad (and via) centre straight to
    where it leaves the copper, so most of a loop seated there is not counted.
    """
    host = _routes()[P_ID].segments[1]
    before = _measured_length(_routes()[P_ID])
    gains = []
    for t in _anchors(host):
        bump = _flush_bump(host, "inner", t)
        assert _partner_clearance(bump) >= GAP
        assert all(_pad_clearance(bump, pad) >= POWER_CLEARANCE for pad in FOREIGN_PADS)
        gains.append(_measured_length(_with_bump(1, bump)) - before)
    loop_added = _config().loop_length_added(W, _amp())
    assert loop_added == pytest.approx(0.1045, abs=1e-4)
    assert max(gains) == pytest.approx(0.0697, abs=2e-3)
    assert max(gains) < loop_added


def test_stub_7_is_blocked_by_j1_s1_and_the_partner() -> None:
    host = _routes()[P_ID].segments[7]
    outer = max(_pad_clearance(_flush_bump(host, "outer", t), _J1_S1) for t in _anchors(host))
    assert outer == pytest.approx(0.1735, abs=1e-3)
    assert outer < POWER_CLEARANCE
    inner = max(_partner_clearance(_flush_bump(host, "inner", t)) for t in _anchors(host))
    assert inner < GAP


@pytest.mark.parametrize(("idx", "t"), [(8, 0.10), (16, 0.10), (17, 0.02)])
def test_stubs_8_16_17_take_one_gated_chamfered_loop(
    idx: int, t: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gates, "MAX_EXCURSION_GAPS", 1.0)
    routes = _routes()
    host = routes[P_ID].segments[idx]
    bump = _flush_bump(host, "outer", t)
    # Clearance: partner at the gap, foreign copper at its class clearance.
    assert _partner_clearance(bump) >= GAP
    assert _route_clearance(bump, routes[TX1N_ID]) >= USB3_CLEARANCE
    assert all(_pad_clearance(bump, pad) >= POWER_CLEARANCE for pad in FOREIGN_PADS)
    assert _usb3_class_gate(routes)(bump, P_ID)
    # Excursion from the untuned route: within 1x the gap.
    original = routes[P_ID].segments
    excursion = max(
        min(point_to_segment_distance(x, y, o.x1, o.y1, o.x2, o.y2) for o in original)
        for s in bump
        for x, y in ((s.x1, s.y1), (s.x2, s.y2))
    )
    assert excursion <= gates.max_excursion_mm(GAP) + 1e-6
    # Q16: the crown is at least 3W.  Q32: every corner of the bump is 45
    # degrees, and the leads keep the feet apart from the route's bends.
    crowns = [s for s in bump[1:-1] if _turn(s, host) < 1e-3 and _length(s) > 0.5]
    assert crowns and all(_length(s) + 1e-6 >= MIN_JOG_WIDTHS * W for s in crowns)
    for a, b in zip(bump, bump[1:], strict=False):
        assert _turn(a, b) == pytest.approx(45.0, abs=1e-3) or _turn(a, b) < 1e-3
    lead_in, lead_out = _length(bump[0]), _length(bump[-1])
    assert min(lead_in, lead_out) >= SKEW_BUMP_FOOT_CHAMFER_MM - 1e-9
    assert _measured_length(_with_bump(idx, bump)) - _measured_length(routes[P_ID]) == (
        pytest.approx(0.1045, abs=1e-3)
    )


def test_body_host_takes_two_flush_loops_not_six() -> None:
    """The 11.794 mm host: ``USB3_TX1-`` leaves room for 2 flush loops at its far end."""
    routes = _routes()
    host = routes[P_ID].segments[11]
    gate = _usb3_class_gate(routes)
    cfg = _config()
    assert int((_length(host) + _jog(cfg)) / (_footprint(1, cfg) + _jog(cfg))) == 6
    two = _flush_bump(host, "outer", 8.54, loops=2)
    assert gate(two, P_ID) and _partner_clearance(two) >= GAP
    assert not any(
        gate(_flush_bump(host, "outer", t, loops=3), P_ID)
        for t in _anchors(host, loops=3, step=0.02)
    )


def test_one_x_best_case_with_pad_end_hosts_still_fails_both_limits() -> None:
    """Every host at its flush-seated maximum, the measured pad-end limits applied.

    Measured on the board (``kct check``'s length model): 29 loops add
    2.997 mm and leave 0.680 mm.  This is the same sum on the fixture.
    """
    cfg = _config()
    segs = _routes()[P_ID].segments
    loop_added = cfg.loop_length_added(W, _amp())

    def flush_max(seg: Segment) -> int:
        return max(
            (k for k in range(1, 40) if _footprint(k, cfg) <= _length(seg) + 1e-9), default=0
        )

    loops = {i: flush_max(s) for i, s in enumerate(segs)}
    # Hosts whose flush maximum the gates cut: the J1 fan stub (J1.B12 / the
    # pads' copper, measured below), stub 7 (J1.S1), the 11.794 mm body host
    # (USB3_TX1-, #7882).
    assert (loops[1], loops[7], loops[11]) == (1, 1, 6)
    loops[1], loops[7], loops[11] = 0, 0, 2
    assert {i: k for i, k in loops.items() if k} == {
        6: 2,
        8: 1,
        10: 1,
        11: 2,
        12: 1,
        13: 12,
        14: 6,
        15: 1,
        16: 1,
        17: 1,
    }
    stub_1_measured = 0.0697
    added = sum(loops.values()) * loop_added + stub_1_measured
    best = _need() + TOLERANCE - added
    assert added == pytest.approx(2.997, abs=0.01)
    assert best == pytest.approx(0.684, abs=0.01)
    assert best > PROPOSED_LIMIT > TOLERANCE
    # The binding constraint is the body host's neighbour, not the pad end:
    # its 4 lost loops would have closed the pair.
    assert best - 4 * loop_added < TOLERANCE


def test_bend_spanning_bump_does_not_beat_straight_loops() -> None:
    """A bump around a convex 45-degree bend gains the corner, but costs more host.

    1x excursion is measured from the untuned route, so at the miter the
    amplitude is ``limit * cos(22.5 deg)``.  The two convex bends with room
    on both sides (12->13 and 14->15) each end ~0.04 mm worse than the
    straight loops the same host holds.
    """
    cfg = _config()
    amp = _amp()
    half = math.radians(22.5)
    a_bend = 1.0 * GAP * math.cos(half)  # the 1x limit, at the miter
    assert a_bend < amp
    s = cfg.leg_advance_mm(W, a_bend)
    leg = s * math.sqrt(2.0) + (a_bend - s) - s
    bend_added = 2 * leg + 2 * a_bend * math.tan(half)
    per_side = s + MIN_JOG_WIDTHS * W - a_bend * math.tan(half)
    loop_added = cfg.loop_length_added(W, amp)
    jog = _jog(cfg)

    def straight(length: float) -> int:
        return max((k for k in range(40) if k == 0 or _footprint(k, cfg) <= length), default=0)

    segs = _routes()[P_ID].segments
    for a, b in ((12, 13), (14, 15)):
        la, lb = _length(segs[a]), _length(segs[b])
        plain = (straight(la) + straight(lb)) * loop_added
        bent = bend_added + loop_added * (
            straight(la - per_side - jog) + straight(lb - per_side - jog)
        )
        assert bent < plain
        assert plain - bent == pytest.approx(0.04, abs=0.01)
