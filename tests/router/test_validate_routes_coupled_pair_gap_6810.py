"""``validate_routes`` holds coupled P/N traces to the pair gap (issue #6810).

The router's pre-save clearance check resolved every different-net
segment pair through ``_get_pair_clearance`` -- ``max(class clearance)`` --
so the P and N copper of an engaged coupled pair, laid at its own floored
diff-pair gap, was held to the class clearance instead.  On 06rf (class
``MIPI``: clearance 0.15, intra-pair 0.1 floored to 0.1016 on jlcpcb) that
was 53 false ``segment`` violations and ``kct route`` exit 3, while
``kct check`` and kicad-cli (whose DRU exempts the pair and adds
``diff_pair_gap``) both passed.

The fix uses the DRU's own pair set (``coupled_engaged_pairs``) and gap
(``_floored_pair_gap`` -> ``DesignRules.pair_gap``) for trace-to-trace
pairs only; pads, vias and every other net pair are unchanged.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import kept_copper_clearance_violations, validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

CLASS_CLEARANCE = 0.15
INTRA = 0.1
FLOORED_GAP = 0.1016  # jlcpcb 4-layer minimum clearance (4 mil), #6443 / #6561
TRACE_W = 0.2

P, N, FOREIGN, KP, KN = 1, 2, 3, 4, 5
NAMES = {
    P: "MIPI_D0+",
    N: "MIPI_D0-",
    FOREIGN: "MIPI_CLK+",
    KP: "ISENSE+",
    KN: "ISENSE-",
}


def _mipi() -> NetClassRouting:
    return NetClassRouting(
        name="MIPI",
        trace_width=TRACE_W,
        clearance=CLASS_CLEARANCE,
        intra_pair_clearance=INTRA,
        coupled_routing=True,
    )


def _router(*, isense_class: bool = False) -> Autorouter:
    rules = DesignRules(
        trace_width=TRACE_W,
        trace_clearance=CLASS_CLEARANCE,
        via_clearance=CLASS_CLEARANCE,
        grid_resolution=0.05,
        manufacturer="jlcpcb",
        # The stackup-aware jlcpcb floor ``kct route`` resolves for 06rf's
        # 4-layer board (#6561); without it the flat profile floor applies.
        mfr_clearance_floor=FLOORED_GAP,
    )
    router = Autorouter(width=40, height=40, rules=rules)
    router.net_names = dict(NAMES)
    mipi = _mipi()
    ncm = {"MIPI_D0+": mipi, "MIPI_D0-": mipi, "MIPI_CLK+": mipi}
    if isense_class:
        # A class on the Kelvin pair but WITHOUT ``coupled_routing``: the
        # router never engages it, so it keeps the full clearance (#4178).
        sense = NetClassRouting(
            name="SENSE",
            trace_width=TRACE_W,
            clearance=CLASS_CLEARANCE,
            intra_pair_clearance=INTRA,
        )
        ncm["ISENSE+"] = sense
        ncm["ISENSE-"] = sense
    router.net_class_map = ncm
    return router


def _seg(net: int, x1: float, y1: float, x2: float, y2: float) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=TRACE_W, layer=Layer.F_CU, net=net)


def _route(net: int, *segs: Segment) -> Route:
    return Route(net=net, net_name=NAMES[net], segments=list(segs), vias=[])


def _pitch(gap: float) -> float:
    """Centerline offset for an edge-to-edge ``gap`` between two traces."""
    return gap + TRACE_W


def _parallel(router: Autorouter, net_a: int, net_b: int, gap: float) -> None:
    y = 10.0
    router.routes = [
        _route(net_a, _seg(net_a, 5.0, y, 25.0, y)),
        _route(net_b, _seg(net_b, 5.0, y + _pitch(gap), 25.0, y + _pitch(gap))),
    ]


def _segment_violations(router: Autorouter) -> list:
    return [v for v in validate_routes(router) if v.obstacle_type == "segment"]


# 1. Coupled pair at its floored gap: the intended geometry, no violation.
def test_coupled_pair_at_floored_gap_is_clean() -> None:
    router = _router()
    _parallel(router, P, N, FLOORED_GAP)
    assert _segment_violations(router) == []


# 2. A skew-tuning style jog on one leg, still at the pair gap: no violation.
def test_coupled_pair_with_meander_jog_at_gap_is_clean() -> None:
    router = _router()
    y = 10.0
    pitch = _pitch(FLOORED_GAP)
    # N runs straight; P (on the outside) bumps out by a meander and back,
    # every piece of it at least the pair gap from N.
    router.routes = [
        _route(
            P,
            _seg(P, 5.0, y, 12.0, y),
            _seg(P, 12.0, y, 12.0, y - 0.5),
            _seg(P, 12.0, y - 0.5, 13.0, y - 0.5),
            _seg(P, 13.0, y - 0.5, 13.0, y),
            _seg(P, 13.0, y, 25.0, y),
        ),
        _route(N, _seg(N, 5.0, y + pitch, 25.0, y + pitch)),
    ]
    assert _segment_violations(router) == []


# 3. No masking: below the pair gap is still flagged, at the pair gap.
def test_coupled_pair_below_gap_still_flagged_at_pair_gap() -> None:
    router = _router()
    _parallel(router, P, N, 0.08)
    found = _segment_violations(router)
    assert found, "a coupled pair below its gap must still be reported"
    for v in found:
        assert {v.net, v.obstacle_net} == {P, N}
        assert v.required == pytest.approx(FLOORED_GAP)
        assert v.distance == pytest.approx(0.08)
        assert not v.component_inherent
        assert not v.is_short


# 4. P against a foreign MIPI-class net keeps the class clearance.
def test_foreign_same_class_net_keeps_class_clearance() -> None:
    router = _router()
    _parallel(router, P, FOREIGN, FLOORED_GAP)
    found = _segment_violations(router)
    assert found
    assert all(v.required == pytest.approx(CLASS_CLEARANCE) for v in found)


# 5. A +/- named pair the router does not engage keeps the full clearance.
@pytest.mark.parametrize("isense_class", [False, True], ids=["no-class", "uncoupled-class"])
def test_unengaged_plus_minus_pair_keeps_full_clearance(isense_class: bool) -> None:
    router = _router(isense_class=isense_class)
    _parallel(router, KP, KN, FLOORED_GAP)
    found = _segment_violations(router)
    assert found
    assert all(v.required == pytest.approx(CLASS_CLEARANCE) for v in found)


# 6. P/N overlap is still a short.
def test_coupled_pair_overlap_is_still_a_short() -> None:
    router = _router()
    _parallel(router, P, N, -0.05)
    found = _segment_violations(router)
    assert found
    assert any(v.is_short for v in found)


# 7. A pad gets no exemption: an N pad near a P trace keeps the class clearance.
def test_pair_pad_keeps_class_clearance() -> None:
    router = _router()
    pad_w = 0.6
    y = 10.0
    pad_y = y + TRACE_W / 2 + 0.12 + pad_w / 2  # 0.12 mm edge gap
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 15.0,
                "y": pad_y,
                "width": pad_w,
                "height": pad_w,
                "net": N,
                "net_name": NAMES[N],
                "shape": "rect",
                "through_hole": False,
                "drill": 0.0,
            }
        ],
    )
    router.routes = [_route(P, _seg(P, 5.0, y, 25.0, y))]
    found = [v for v in validate_routes(router) if v.obstacle_type == "pad"]
    assert found
    assert all(v.required == pytest.approx(CLASS_CLEARANCE) for v in found)
    assert all(v.distance == pytest.approx(0.12) for v in found)


# The kept-copper audit's fallback (no board rules) applies the same view.
def test_kept_copper_audit_fallback_uses_pair_gap() -> None:
    router = _router()
    _parallel(router, P, N, FLOORED_GAP)
    router.existing_routes = router.routes
    router.routes = []
    try:
        found = kept_copper_clearance_violations(router)
    except Exception as exc:  # pragma: no cover - surface fixture drift clearly
        pytest.fail(f"kept-copper audit failed: {exc}")
    assert [v for v in found if {v.net, v.obstacle_net} == {P, N}] == []
