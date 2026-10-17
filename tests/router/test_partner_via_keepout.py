"""Partner-via keep-out uses the trace/via floor the exact gate enforces (Issue #6963).

Epic #5509 phase 4e slice 3.  The coupled shadow constructor pre-screens
candidate via sites against the partner leg with a centre-to-centreline
keep-out.  It used ``trace_clearance``; the exact gate it feeds
(``_via_copper_deficit``) uses ``max(trace_clearance, via_clearance)``.  When
``via_clearance > trace_clearance`` the pre-screen admitted sites the gate then
rejected.  These tests pin the helper's value, tie it to the gate as an
oracle, and assert the default (shadow-off) path never compares against it.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.diffpair_routing import (
    _SHADOW_VIA_DEFICIT_EPS,
    StubEdgeSpec,
    _via_to_trace_keepout_mm,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

_TRACE = 0.15
_VIA = 0.20
_VIA_D = 0.45
_VIA_DRILL = 0.25
_W = 0.15


def _rules(trace: float = _TRACE, via: float = _VIA) -> DesignRules:
    return DesignRules(
        trace_clearance=trace,
        via_clearance=via,
        via_diameter=_VIA_D,
        via_drill=_VIA_DRILL,
        trace_width=_W,
    )


# ---------------------------------------------------------------------------
# Value pin
# ---------------------------------------------------------------------------


def test_keepout_uses_the_trace_via_floor_when_via_clearance_is_larger():
    assert _via_to_trace_keepout_mm(_rules(), _W) == pytest.approx(0.50)
    old = _VIA_D / 2 + _TRACE + _W / 2
    assert old == pytest.approx(0.45)


def test_keepout_matches_old_formula_when_floors_are_equal():
    rules = _rules(trace=0.15, via=0.15)
    assert _via_to_trace_keepout_mm(rules, _W) == pytest.approx(0.45)
    assert _via_to_trace_keepout_mm(rules, _W) == pytest.approx(_VIA_D / 2 + 0.15 + _W / 2)


def test_keepout_floor_is_symmetric_when_trace_clearance_is_larger():
    rules = _rules(trace=0.20, via=0.15)
    assert _via_to_trace_keepout_mm(rules, _W) == pytest.approx(0.225 + 0.2 + 0.075)
    assert _via_to_trace_keepout_mm(rules, _W) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Oracle: agreement with the exact gate
# ---------------------------------------------------------------------------


def _router(rules: DesignRules):
    from kicad_tools.router.core import Autorouter

    rules.grid_resolution = 0.05
    return Autorouter(width=20.0, height=10.0, rules=rules)._diffpair


_PARTNER_Y = 5.0


def _partner_route() -> Route:
    route = Route(net=8, net_name="USB3_TX1-")
    route.segments.append(
        Segment(
            x1=3.0,
            y1=_PARTNER_Y,
            x2=7.0,
            y2=_PARTNER_Y,
            width=_W,
            layer=Layer.F_CU,
            net=8,
            net_name="USB3_TX1-",
        )
    )
    return route


def _via_at_offset(k: float) -> Via:
    return Via(
        x=5.0,
        y=_PARTNER_Y + k,
        drill=_VIA_DRILL,
        diameter=_VIA_D,
        layers=(Layer.F_CU, Layer.B_CU),
        net=7,
        net_name="USB3_TX1+",
    )


def _gate_deficit(dpr, k: float) -> float:
    with dpr._shadow_foreign_copper(_partner_route()):
        deficit, _loc = dpr._via_copper_deficit(_via_at_offset(k))
    return deficit


@pytest.fixture
def dpr_and_keepout():
    rules = _rules()
    dpr = _router(rules)
    return dpr, _via_to_trace_keepout_mm(dpr.autorouter.rules, _W)


def test_gate_accepts_just_outside_the_keepout(dpr_and_keepout):
    dpr, keepout = dpr_and_keepout
    k = keepout + 1e-4
    assert k >= keepout  # pre-screen accepts
    assert _gate_deficit(dpr, k) <= _SHADOW_VIA_DEFICIT_EPS  # gate accepts


def test_gate_rejects_just_inside_the_keepout(dpr_and_keepout):
    dpr, keepout = dpr_and_keepout
    k = keepout - 1e-3
    assert k < keepout  # pre-screen rejects
    assert _gate_deficit(dpr, k) > _SHADOW_VIA_DEFICIT_EPS  # gate rejects


def test_old_formula_admitted_a_site_the_gate_rejects(dpr_and_keepout):
    """The bug being fixed: 0.47 mm sits between the old 0.45 and new 0.50 bound."""
    dpr, keepout = dpr_and_keepout
    old = 0.45 / 2 + 0.15 + 0.15 / 2
    k = 0.47
    assert k >= old  # old pre-screen accepted it
    assert k < keepout  # new pre-screen rejects it
    assert _gate_deficit(dpr, k) > _SHADOW_VIA_DEFICIT_EPS  # and so does the gate


# ---------------------------------------------------------------------------
# Shadow-off reach: the stub-edge crossing tail compares against no partner
# ---------------------------------------------------------------------------


def test_stub_edge_crossing_tail_passes_no_partner_segments(monkeypatch):
    dpr = _router(_rules())
    start = Pad(
        x=4.0, y=5.0, width=0.3, height=0.3, layer=Layer.F_CU, net=7,
        net_name="USB_D+", ref="J1", pin="A6",
    )  # fmt: skip
    end = Pad(
        x=4.6, y=5.0, width=0.3, height=0.3, layer=Layer.F_CU, net=7,
        net_name="USB_D+", ref="J1", pin="B6",
    )  # fmt: skip

    monkeypatch.setattr(dpr.autorouter.router, "route", lambda *_a, **_k: None)
    monkeypatch.setattr(dpr, "_synthesize_tail", lambda *_a, **_k: None)
    calls: list[tuple[tuple, dict]] = []

    def _capture(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr(dpr, "_synthesize_crossing_tail", _capture)

    dpr._route_stub_edges([StubEdgeSpec(start=start, end=end)])

    assert len(calls) == 1
    args, kwargs = calls[0]
    partner_segments = kwargs.get("partner_segments", args[4] if len(args) > 4 else None)
    assert partner_segments == []
