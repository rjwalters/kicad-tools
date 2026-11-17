"""Egress-aware lattice pad escape on fine-pitch rows (issue #6381).

Board 05 PHASE_B (U2.8 -> J2.2) declined under ``--route-engine lattice``
even on an otherwise empty board.  U2.8 is the middle pad of a 0.65 mm pitch
QFN row on a 0.4 mm / 0.2 mm-clearance class, so the legal full-width
centreline band between its neighbours is only 0.1 mm tall.  When no lattice
row falls inside that band, the only nodes a full-width stub can reach are
cell centres INSIDE the pad's own copper, and every edge out of them is
blocked.  Those dead-end stubs used to count as a successful full-width
escape, so the #4293 neck-down fallback never ran and the A* popped the
stubs and declined -- labelled ``no-path-layer-constrained`` whenever the
far pad's inner-layer stubs had been pruned, which pointed the
investigation at plane reservation instead.

These tests pin:

* (a) ``_escape_stubs`` returns the neck width, not dead-end full-width stubs;
* (b) ``route()`` returns a route;
* (c) every emitted segment clears the neighbour pads at class clearance;
* (d) the outcome is independent of the lattice phase (half-cell shifts);
* the decline label names the dead-end escape, not the layer constraint;
* a pad whose full-width stubs DO egress is returned unchanged.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, box

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

_OUTLINE = [(0.0, 0.0), (8.0, 0.0), (8.0, 8.0), (0.0, 8.0)]
_CLASS = NetClassRouting(name="HC", trace_width=0.4, clearance=0.2)
_PITCH = 0.65

# Row-centre y values.  At 4.1 no lattice row falls in the 0.1 mm full-width
# band (the pre-#6381 failure); 4.0 and 4.2 are the half-cell-shifted phases
# where one does.  4.075/4.125 are the band edges of the failing phase.
_DEAD_END_PHASES = (4.075, 4.1, 4.125)
_FULL_WIDTH_PHASES = (4.0, 4.2)


def _row(cy: float, *, far_pad: Pad | None = None) -> list[Pad]:
    """Three 1.575 x 0.4 roundrect pads at 0.65 pitch; the middle one is net 1."""
    pads = [
        Pad(
            x=6.0,
            y=cy + (i - 1) * _PITCH,
            width=1.575,
            height=0.4,
            net=net,
            net_name=name,
            layer=Layer.F_CU,
            ref="U1",
            pin=str(i + 1),
            shape="roundrect",
        )
        for i, (net, name) in enumerate([(2, "GND_SENSE"), (1, "PHASE_B"), (3, "PHASE_C")])
    ]
    pads.append(
        far_pad
        if far_pad is not None
        else Pad(
            x=1.5,
            y=cy,
            width=0.6,
            height=0.6,
            net=1,
            net_name="PHASE_B",
            layer=Layer.F_CU,
            ref="J1",
            pin="1",
        )
    )
    return pads


def _pathfinder(pads: list[Pad], **kwargs) -> LatticePathfinder:
    return LatticePathfinder(
        _OUTLINE,
        pads,
        kwargs.pop("rules", DesignRules(trace_width=0.2, trace_clearance=0.2)),
        layer_stack=kwargs.pop("layer_stack", LayerStack.two_layer()),
    )


def _escape(pf: LatticePathfinder, pad: Pad, net_class=_CLASS):
    return pf._escape_stubs(
        pad,
        pad.net,
        None,
        kmax=4,
        extra_clearance=0.0,
        partner_net=None,
        layers=None,
        exempt_pads=None,
        net_class=net_class,
    )


def _pad_box(pad: Pad):
    return box(
        pad.x - pad.width / 2,
        pad.y - pad.height / 2,
        pad.x + pad.width / 2,
        pad.y + pad.height / 2,
    )


@pytest.mark.parametrize("cy", _DEAD_END_PHASES)
def test_dead_end_full_width_stubs_fall_back_to_neck(cy: float) -> None:
    """(a) No full-width stub has egress -> the neck width is returned."""
    pads = _row(cy)
    pf = _pathfinder(pads)
    full = pf.pad_stubs(pads[1], 1, None, net_class=_CLASS)
    assert full, "the full-width scan does find (in-pad) stubs"
    half, clr = pf._conn_geometry(_CLASS, 1)
    assert not any(pf._stub_has_egress(s, 1, None, half, clr, net_class=_CLASS) for s in full), (
        "precondition: every full-width stub is a dead end at this lattice phase"
    )
    stubs, width = _escape(pf, pads[1])
    assert width == pytest.approx(0.2)
    assert stubs
    assert any(pf._stub_has_egress(s, 1, None, half, clr, net_class=_CLASS) for s in stubs)


@pytest.mark.parametrize("cy", _DEAD_END_PHASES + _FULL_WIDTH_PHASES)
def test_fine_pitch_middle_pad_routes_in_every_phase(cy: float) -> None:
    """(b)+(c)+(d) The pad routes in every lattice phase and clears its neighbours."""
    pads = _row(cy)
    pf = _pathfinder(pads)
    route = pf.route(pads[1], pads[3], net_class=_CLASS)
    assert route is not None, f"fine-pitch middle pad must escape at lattice phase y={cy}"
    assert route.segments
    widths = {round(s.width, 4) for s in route.segments}
    assert widths <= {0.2, 0.4}
    assert 0.4 in widths, "the body is emitted at the full class width"
    for seg in route.segments:
        copper = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)]).buffer(
            seg.width / 2, quad_segs=64
        )
        for neighbour in (pads[0], pads[2]):
            # The pad's bounding box conservatively contains its rounded copper.
            assert copper.distance(_pad_box(neighbour)) >= 0.2 - 1e-6, (
                f"segment {seg} at width {seg.width} violates class clearance "
                f"to {neighbour.net_name}"
            )


@pytest.mark.parametrize("cy", _FULL_WIDTH_PHASES)
def test_full_width_escape_unchanged_when_stubs_egress(cy: float) -> None:
    """A pad whose full-width stubs already egress keeps them byte-identically."""
    pads = _row(cy)
    pf = _pathfinder(pads)
    full = pf.pad_stubs(pads[1], 1, None, net_class=_CLASS)
    stubs, width = _escape(pf, pads[1])
    assert width == pytest.approx(0.4)
    assert stubs == full


def test_dead_end_without_neck_keeps_full_width_stubs() -> None:
    """When the neck is no narrower than the body, the old result stands."""
    no_neck = NetClassRouting(name="HC", trace_width=0.4, clearance=0.2, neck_trace_width=0.4)
    pads = _row(4.1)
    pf = _pathfinder(pads)
    full = pf.pad_stubs(pads[1], 1, None, net_class=no_neck)
    stubs, width = _escape(pf, pads[1], net_class=no_neck)
    assert width == pytest.approx(0.4)
    assert stubs == full


def test_dead_end_decline_is_not_labelled_layer_constrained() -> None:
    """The decline names the dead-end start escape, not the far pad's pruned layers.

    The far pad is a through-hole pad on a 4-layer board whose class hard-
    avoids the inner layers, so its In1/In2 stubs are dropped (the pruning
    that used to set ``no-path-layer-constrained``).  The start pad has no
    neck available (``neck_trace_width`` == body width), so its dead-end
    full-width stubs are the whole story.
    """
    strict = NetClassRouting(
        name="HC",
        trace_width=0.4,
        clearance=0.2,
        neck_trace_width=0.4,
        preferred_layers=[0, 3],
        avoid_layers=[1, 2],
    )
    far = Pad(
        x=1.5,
        y=4.1,
        width=1.2,
        height=1.2,
        net=1,
        net_name="PHASE_B",
        layer=Layer.F_CU,
        ref="J1",
        pin="1",
        through_hole=True,
        drill=0.6,
    )
    pads = _row(4.1, far_pad=far)
    pf = _pathfinder(
        pads,
        rules=DesignRules(trace_width=0.2, trace_clearance=0.2, strict_layers=True),
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    assert pf._hard_avoided_layers(strict) == frozenset({1, 2})
    result, reason = pf._route_impl(
        pads[1],
        far,
        strict,
        committed=pf._fresh_committed(),
        history={},
        present=0.0,
    )
    assert result is None
    assert reason == "no-egress-start"
