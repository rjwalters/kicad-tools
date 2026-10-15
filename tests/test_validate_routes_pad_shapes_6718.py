"""``validate_routes`` measures pads as their real copper (issue #6718).

The router's pre-save clearance check used to hold every pad to its full
(residual-rotated) rectangle.  A trace passing a round pad's corner read
``0.35 * (sqrt(2) - 1) = 0.145`` mm closer than it really was, so board 03's
USB-C ``J1`` tie stubs (``thru_hole circle 0.7 x 0.7``) reported 0.006 mm
``[pad]`` violations that kicad-cli and ``kct check`` both pass.  Those
results feed rip-up/reroute, the short gate and the DRC-nudge repair.

Both pad quadrants (segment vs pad, via vs pad) now ask the shared clearance
kernel (:mod:`kicad_tools.router.clearance_shapes`), the model DRC uses.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.clearance_shapes import copper_gap, pad_shape, segment_shape, via_shape
from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

CLEARANCE = 0.15
TRACE_W = 0.2
VIA_D = 0.6
TRUE_GAP = 0.151  # the board-03 J1 tie: just over the 0.15 mm requirement
PX, PY = 10.0, 10.0  # pad centre

# Unit normal of a 45-degree approach (towards the pad's upper-right corner).
_N = (1.0 / math.sqrt(2.0), 1.0 / math.sqrt(2.0))


def _router() -> Autorouter:
    rules = DesignRules(
        trace_width=TRACE_W,
        trace_clearance=CLEARANCE,
        via_clearance=CLEARANCE,
        grid_resolution=0.05,
    )
    router = Autorouter(width=20, height=20, rules=rules)
    router.net_names = {1: "SIG", 2: "OTHER"}
    return router


def _add_pad(router: Autorouter, shape: str, width: float, height: float) -> Pad:
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": PX,
                "y": PY,
                "width": width,
                "height": height,
                "net": 2,
                "net_name": "OTHER",
                "shape": shape,
                "through_hole": True,
                "drill": 0.3,
            }
        ],
    )
    return router.pads[("J1", "1")]


def _support(shape: str, width: float, height: float) -> float:
    """How far the pad's copper reaches along ``_N`` from its centre."""
    hw, hh = width / 2.0, height / 2.0
    if shape == "rect":
        return hw * _N[0] + hh * _N[1]
    if shape == "circle":
        return hw
    if shape == "oval":  # stadium: end-cap circles of radius min/2
        r = min(hw, hh)
        return (hw - r) * _N[0] + (hh - r) * _N[1] + r
    if shape == "roundrect":  # KiCad default ratio 0.25 of the short side
        r = 0.25 * min(width, height)
        return (hw - r) * _N[0] + (hh - r) * _N[1] + r
    raise AssertionError(shape)


def _track_at_gap(shape: str, width: float, height: float, gap: float) -> Segment:
    """A 45-degree track whose copper edge sits ``gap`` off the pad's copper."""
    d = _support(shape, width, height) + gap + TRACE_W / 2.0
    cx, cy = PX + d * _N[0], PY + d * _N[1]
    # Run perpendicular to the normal, 1 mm each side of the tangent point.
    tx, ty = _N[0], -_N[1]
    return Segment(x1=cx - tx, y1=cy - ty, x2=cx + tx, y2=cy + ty, layer=Layer.F_CU, width=TRACE_W)


def _pad_violations(router: Autorouter, route: Route) -> list:
    router.routes.append(route)
    return [v for v in validate_routes(router) if v.obstacle_type == "pad"]


# ---------------------------------------------------------------------------
# Segment vs pad
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "width", "height"),
    [
        ("circle", 0.7, 0.7),
        ("oval", 0.9, 1.7),
    ],
)
def test_track_past_a_rounded_pad_at_a_legal_gap_is_clean(
    shape: str, width: float, height: float
) -> None:
    router = _router()
    pad = _add_pad(router, shape, width, height)
    seg = _track_at_gap(shape, width, height, TRUE_GAP)
    # The geometry really is 0.151 mm from the pad's copper ...
    assert copper_gap(segment_shape(seg), pad_shape(pad)) == pytest.approx(TRUE_GAP, abs=1e-6)
    # ... but under the pad's bounding rectangle it would be a violation.
    rect_gap = TRUE_GAP + _support(shape, width, height) - _support("rect", width, height)
    assert rect_gap < CLEARANCE
    assert _pad_violations(router, Route(net=1, net_name="SIG", segments=[seg], vias=[])) == []


def test_track_past_a_roundrect_pad_corner_is_not_hidden_by_assumed_ratio() -> None:
    """A roundrect pad is measured as its rectangle (the router has no rratio).

    A 2.0 x 2.0 pad with file ratio 0.1 has a 0.2 mm corner radius; the
    kernel's default 0.25 would model a 0.5 mm radius and read a real
    0.10 mm near-miss past the corner as ~0.22 mm, hiding the violation.
    """
    router = _router()
    _add_pad(router, "roundrect", 2.0, 2.0)
    # Place the track 0.10 mm off the true (ratio 0.1) corner.
    r = 0.1 * 2.0
    support = (1.0 - r) * (_N[0] + _N[1]) + r
    d = support + 0.10 + TRACE_W / 2.0
    cx, cy = PX + d * _N[0], PY + d * _N[1]
    tx, ty = _N[0], -_N[1]
    seg = Segment(x1=cx - tx, y1=cy - ty, x2=cx + tx, y2=cy + ty, layer=Layer.F_CU, width=TRACE_W)
    found = _pad_violations(router, Route(net=1, net_name="SIG", segments=[seg], vias=[]))
    assert len(found) == 1
    assert found[0].distance < CLEARANCE


def test_via_past_a_roundrect_pad_corner_is_not_hidden_by_assumed_ratio() -> None:
    router = _router()
    _add_pad(router, "roundrect", 2.0, 2.0)
    r = 0.1 * 2.0
    support = (1.0 - r) * (_N[0] + _N[1]) + r
    via = _via_at_gap(support, 0.10)
    found = _pad_violations(router, Route(net=1, net_name="SIG", segments=[], vias=[via]))
    assert len(found) == 1
    assert found[0].distance < CLEARANCE


def test_the_same_track_past_a_square_pad_is_a_violation() -> None:
    """The control: a ``rect`` pad's corner really is 0.006 mm away."""
    router = _router()
    _add_pad(router, "rect", 0.7, 0.7)
    # Same track as the circle case: placed off the 0.7 mm circle.
    seg = _track_at_gap("circle", 0.7, 0.7, TRUE_GAP)
    found = _pad_violations(router, Route(net=1, net_name="SIG", segments=[seg], vias=[]))
    assert len(found) == 1
    expected = TRUE_GAP - 0.35 * (math.sqrt(2.0) - 1.0)
    assert found[0].distance == pytest.approx(expected, abs=1e-4)
    assert found[0].distance == pytest.approx(0.006, abs=1e-3)
    assert found[0].required == pytest.approx(CLEARANCE)


def test_track_through_a_round_pad_is_still_a_short() -> None:
    router = _router()
    _add_pad(router, "circle", 0.7, 0.7)
    seg = Segment(x1=PX - 1.0, y1=PY, x2=PX + 1.0, y2=PY, layer=Layer.F_CU, width=TRACE_W)
    found = _pad_violations(router, Route(net=1, net_name="SIG", segments=[seg], vias=[]))
    assert len(found) == 1
    assert found[0].distance <= 0.0


def test_track_grazing_a_round_pad_below_clearance_is_still_reported() -> None:
    """A real near-miss on a round pad is not hidden by the shape-aware gap."""
    router = _router()
    _add_pad(router, "circle", 0.7, 0.7)
    seg = _track_at_gap("circle", 0.7, 0.7, 0.10)
    found = _pad_violations(router, Route(net=1, net_name="SIG", segments=[seg], vias=[]))
    assert len(found) == 1
    assert found[0].distance == pytest.approx(0.10, abs=1e-6)


# ---------------------------------------------------------------------------
# Via vs pad
# ---------------------------------------------------------------------------


def _via_at_gap(support: float, gap: float) -> Via:
    d = support + gap + VIA_D / 2.0
    return Via(
        x=PX + d * _N[0],
        y=PY + d * _N[1],
        drill=0.3,
        diameter=VIA_D,
        layers=(Layer.F_CU, Layer.B_CU),
        net=1,
        net_name="SIG",
    )


def test_via_past_a_round_pad_at_a_legal_gap_is_clean() -> None:
    router = _router()
    pad = _add_pad(router, "circle", 0.7, 0.7)
    via = _via_at_gap(0.35, TRUE_GAP)
    assert copper_gap(via_shape(via), pad_shape(pad)) == pytest.approx(TRUE_GAP, abs=1e-6)
    assert _pad_violations(router, Route(net=1, net_name="SIG", segments=[], vias=[via])) == []


def test_the_same_via_past_a_square_pad_is_a_violation() -> None:
    router = _router()
    _add_pad(router, "rect", 0.7, 0.7)
    via = _via_at_gap(0.35, TRUE_GAP)
    found = _pad_violations(router, Route(net=1, net_name="SIG", segments=[], vias=[via]))
    assert len(found) == 1
    assert found[0].distance == pytest.approx(0.006, abs=1e-3)


def test_via_on_a_round_pad_is_still_a_short() -> None:
    router = _router()
    _add_pad(router, "circle", 0.7, 0.7)
    via = Via(
        x=PX + 0.3,
        y=PY,
        drill=0.3,
        diameter=VIA_D,
        layers=(Layer.F_CU, Layer.B_CU),
        net=1,
        net_name="SIG",
    )
    found = _pad_violations(router, Route(net=1, net_name="SIG", segments=[], vias=[via]))
    assert len(found) == 1
    assert found[0].distance <= 0.0
