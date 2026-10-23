"""Issue #7869: length-balanced crossing USB-C row-to-row ties.

A mirrored USB-C receptacle (board 06rf J1: A6/B6 D+, A7/B7 D-) puts the
pair's two same-net row-to-row ties across each other.  On one layer only one
of them can take the direct dogleg; the other has to go round its partner's
pad, ~2.3 mm longer on 06rf -- skew the tuner cannot remove within its bounds.
:func:`plan_crossing_ties` plans both: the blocked net round the partner pad,
the partner padded to the same length with plain 45-degree-chamfered copper.

The skew budget the ties are held to is :data:`CROSSING_TIE_MATCH_MM`
(0.05 mm), a small fraction of the tightest pair limit it serves (USB 2.0 HS,
1.0 mm), so the pair leaves the connector balanced.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from shapely.geometry import LineString, Point, box

from kicad_tools.router.diffpair_bump_gates import MIN_JOG_WIDTHS
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairplan.tie import (
    CROSSING_TIE_MATCH_MM,
    _ties_uncoupled_mm,
    corner_turns_deg,
    plan_crossing_ties,
)
from kicad_tools.router.primitives import Pad
from tests.router.lattice.test_coupled_pairs import _usbc_pads

#: The tie budget this test holds the planner to, inside the 1.0 mm USB 2.0
#: HS skew limit.
SKEW_BUDGET_MM = 1.0

_CLEARANCE = 0.15


def _rules(width: float) -> dict:
    return {"width": width, "clearance": _CLEARANCE, "min_width": 0.1016, "blocked": None}


def _j1_06rf() -> dict[str, Pad]:
    """Board 06rf J1 (USB_C_Receptacle_USB2.0 at 113.5, 55.5): 0.3 x 0.6 mm
    SMD pads at 1.0 mm pitch, the B row 2.0 mm behind the A row, mirrored.
    The +3V3 pads carry the board's 0.2 mm authored floor (neutralised net)."""
    nets = {
        "A4": (5, "VBUS"),
        "A5": (3, "CC1"),
        "A6": (1, "D+"),
        "A7": (2, "D-"),
        "A8": (4, "+3V3"),
        "A9": (5, "VBUS"),
        "B4": (5, "VBUS"),
        "B5": (6, "CC2"),
        "B6": (1, "D+"),
        "B7": (2, "D-"),
        "B8": (4, "+3V3"),
        "B9": (5, "VBUS"),
    }
    pads = {}
    for pin, (net, name) in nets.items():
        i = int(pin[1:])
        dx = -5.5 + (i - 1) if pin[0] == "A" else 5.5 - (i - 1)
        dy = 0.0 if pin[0] == "A" else 2.0
        pads[pin] = Pad(
            x=113.5 + dx,
            y=55.5 + dy,
            width=0.3,
            height=0.6,
            net=net,
            net_name=name,
            layer=Layer.F_CU,
            ref="J1",
            pin=pin,
            shape="rect",
            authored_clearance=0.2 if name == "+3V3" else 0.0,
        )
    return pads


def _copper(pad: Pad):
    if pad.shape == "circle":
        return Point(pad.x, pad.y).buffer(pad.width / 2.0, quad_segs=64)
    return box(
        pad.x - pad.width / 2.0,
        pad.y - pad.height / 2.0,
        pad.x + pad.width / 2.0,
        pad.y + pad.height / 2.0,
    )


def _tie_copper(tie):
    return LineString(tie.points).buffer(tie.width / 2.0, quad_segs=64)


def _plan(pads: dict[str, Pad], width: float):
    return plan_crossing_ties(
        pads["B7"],
        pads["A7"],
        pads["B6"],
        pads["A6"],
        list(pads.values()),
        [0],
        wrap_rules=_rules(width),
        pad_rules=_rules(width),
    )


def _assert_q32_q16(tie, row_axis: tuple[float, float]) -> None:
    pts = tie.points
    # Every leg octilinear ...
    for a, b in zip(pts, pts[1:], strict=False):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        assert dx < 1e-9 or dy < 1e-9 or abs(dx - dy) < 1e-6, (a, b)
    # ... every vertex a 45 degree corner: no square, no acute corner (Q32).
    turns = corner_turns_deg(pts)
    assert turns and all(t == pytest.approx(45.0, abs=1e-6) for t in turns), turns
    # Q16: every interior leg that is not a 45 degree chamfer -- runs along
    # the rows AND across them, so a hairpin's turnaround (the flat top
    # joining its two runs) included -- is a full jog.  Only the first and
    # last legs (the pad leads) and the diagonals (the Q32 chamfers) are
    # exempt.
    min_flat = MIN_JOG_WIDTHS * tie.class_width
    for a, b in list(zip(pts, pts[1:], strict=False))[1:-1]:
        dx, dy = b[0] - a[0], b[1] - a[1]
        along = abs(dx * row_axis[0] + dy * row_axis[1])
        across = abs(dx * row_axis[1] - dy * row_axis[0])
        if along < 1e-9 or across < 1e-9:
            assert math.hypot(dx, dy) >= min_flat - 1e-6, (a, b, math.hypot(dx, dy))


def _assert_clear(ties, pads: dict[str, Pad]) -> None:
    for tie in ties:
        copper = _tie_copper(tie)
        for pad in pads.values():
            if pad.net != tie.net:
                floor = max(_CLEARANCE, pad.authored_clearance)
                assert copper.distance(_copper(pad)) >= floor - 1e-6, pad.pin
    a, b = ties
    assert _tie_copper(a).distance(_tie_copper(b)) >= _CLEARANCE - 1e-6


@pytest.mark.parametrize(
    ("pads", "width"),
    [
        pytest.param(_usbc_pads(smd=True), 0.2, id="synthetic-usbc"),
        pytest.param(_j1_06rf(), 0.25, id="06rf-J1"),
    ],
)
def test_crossing_ties_are_planned_length_matched(pads, width) -> None:
    got = _plan(pads, width)
    assert got is not None, "both crossing ties are planned"
    wrap, padded = got
    assert (wrap.net, padded.net) == (2, 1)
    # Extra pad centre first, anchor pad centre last.
    assert wrap.points[0] == (pads["B7"].x, pads["B7"].y)
    assert wrap.points[-1] == (pads["A7"].x, pads["A7"].y)
    assert padded.points[0] == (pads["B6"].x, pads["B6"].y)
    assert padded.points[-1] == (pads["A6"].x, pads["A6"].y)
    row = (pads["B6"].x - pads["B7"].x, pads["B6"].y - pads["B7"].y)
    row_axis = (row[0] / math.hypot(*row), row[1] / math.hypot(*row))
    assert CROSSING_TIE_MATCH_MM <= SKEW_BUDGET_MM
    assert abs(wrap.length - padded.length) <= CROSSING_TIE_MATCH_MM
    for tie in got:
        assert tie.width == pytest.approx(width), "class width, not necked"
        assert tie.layer == 0
        assert tie.length == pytest.approx(
            sum(math.dist(p, q) for p, q in zip(tie.points, tie.points[1:], strict=False))
        )
        _assert_q32_q16(tie, row_axis)
    _assert_clear(got, pads)
    # The direct dogleg (1 + sqrt 2 mm on 06rf) is padded by deliberate length.
    direct = math.dist(*[(pads[k].x, pads[k].y) for k in ("B6", "A6")])
    assert padded.length > direct + 1.0


def test_06rf_ties_match_the_wrap_within_the_budget() -> None:
    wrap, padded = _plan(_j1_06rf(), 0.25)
    # The lattice's own D- wrap on 06rf was ~4.75 mm against a 2.4 mm D+ stub
    # (2.33 mm skew); the planned set leaves J1 balanced.
    assert abs(wrap.length - padded.length) <= CROSSING_TIE_MATCH_MM
    assert 4.0 < wrap.length < 6.0


def test_06rf_hairpin_turnaround_is_a_full_q16_jog() -> None:
    # The padded D+ tie turns back between the A row (CC1, 0.15 mm) and the
    # B row (+3V3, 0.2 mm floor).  Its turnaround -- the leg across the rows
    # joining the two runs -- is a Q16 run like any other: >= 3 x 0.25 mm.
    pads = _j1_06rf()
    _wrap, padded = _plan(pads, 0.25)
    legs = list(zip(padded.points, padded.points[1:], strict=False))[1:-1]
    across = [math.dist(a, b) for a, b in legs if abs(a[0] - b[0]) < 1e-9]
    assert across, "the padded tie has a turnaround across the rows"
    assert min(across) >= MIN_JOG_WIDTHS * 0.25 - 1e-6, across


def test_06rf_lengthened_wrap_couples_to_the_padded_tie() -> None:
    # The strict-Q16 hairpin (4.97 mm) is longer than the shortest wrap
    # (4.81 mm), so the wrap is re-planned to its length.  The extra length
    # goes where the two ties run beside each other: the pair's uncoupled
    # figure over the two ties is 3.36 mm, against 4.34 mm for the same
    # wrap lengthened by its lead out of B7 alone (AC 5: no worse than main).
    # Its descent column stays where the shortest wrap's was (B5's escape).
    pads = _j1_06rf()
    wrap, padded = _plan(pads, 0.25)
    assert wrap.length == pytest.approx(padded.length, abs=CROSSING_TIE_MATCH_MM)
    figure = _ties_uncoupled_mm(wrap, padded, list(pads.values()), lambda _p, _l: True, 0)
    lead_lengthened = replace(
        wrap,
        points=(
            (113.0, 57.5),
            (113.0, 57.7707),
            (113.6, 58.3707),
            (114.35, 58.3707),
            (114.45, 58.2707),
            (114.45, 55.95),
            (114.0, 55.5),
        ),
    )
    naive = _ties_uncoupled_mm(lead_lengthened, padded, list(pads.values()), lambda _p, _l: True, 0)
    assert figure < naive - 0.9, (figure, naive)
    descent_x = max(p[0] for p in wrap.points)
    assert descent_x <= 114.45 + 1e-9, wrap.points


def test_non_crossing_ties_are_not_planned_here() -> None:
    # Rows NOT mirrored: the two direct ties are parallel, nothing crosses.
    pads = _usbc_pads(smd=True)
    pads["B6"] = replace(pads["B6"], y=pads["A6"].y)
    pads["B7"] = replace(pads["B7"], y=pads["A7"].y)
    assert _plan(pads, 0.2) is None


def test_crossing_ties_decline_without_room() -> None:
    # A rule area over the whole connector vetoes every leg: no set, no half.
    rules = dict(_rules(0.2), blocked=lambda a, b, layer, half: True)
    pads = _usbc_pads(smd=True)
    assert (
        plan_crossing_ties(
            pads["B7"],
            pads["A7"],
            pads["B6"],
            pads["A6"],
            list(pads.values()),
            [0],
            wrap_rules=rules,
            pad_rules=_rules(0.2),
        )
        is None
    )


def test_crossing_ties_decline_when_the_pads_are_boxed_in() -> None:
    # 06rf J1 with a foreign pad in every gap the wrap could take: no legal
    # wrap, so neither tie (the caller keeps today's connections).
    pads = _j1_06rf()
    walls = {}
    for i, x in enumerate((113.5, 114.5, 112.5)):
        for y in (55.5, 57.5, 56.5, 58.1, 54.9):
            walls[f"W{i}{y}"] = replace(
                pads["A5"], x=x, y=y, width=0.6, height=0.6, net=9, net_name="GND", pin="W"
            )
    assert _plan({**pads, **walls}, 0.25) is None
