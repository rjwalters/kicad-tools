"""Lattice class-pad clearance under a ``--voltage-map`` (issue #7880).

The HV pairwise projection (:class:`~kicad_tools.router.lattice.pairwise.
LatticePairwise`) carries only CROSS-domain pairs.  Before #7880 an active
projection also switched off the two post-negotiation steps that hold a pad's
own router net class -- the class-pad repair (#6395) and the grown-pad
re-negotiation (#6489) -- so a SAME-domain class pair was enforced by nothing
until the router's post-route backstop, which demoted the whole net:

* a 0.2 mm-class net passing a 0.4 mm-class HV pad at 0.3 mm;
* an HV net in the 0.2 mm class passing another HV net's 0.4 mm-class pad at
  0.3 mm (same 300 V domain, so no pairwise entry).

The fixture below is synthetic and public.  An unrelated 0 V net far from the
corridor makes the projection non-``None`` without any HV-to-LV proximity on
it.  On ``main`` both corridor nets were demoted; with the fix the backstop
demotes nothing.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.class_clearance_floors import refresh_router_class_floors
from kicad_tools.router.clearance_shapes import copper_gap, pad_shape, segment_shape
from kicad_tools.router.core import Autorouter
from kicad_tools.router.lattice.obstacles import LatticeObstacleModel
from kicad_tools.router.lattice.pairwise import LatticePairwise
from kicad_tools.router.lattice.quadtree import OctilinearLattice
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

BASE = 0.2  # board-global clearance and the Digital class
HV_CLASS = 0.4
TRACE_W = 0.2
EPS = 1e-6

SIG, HV_B, HV_C, LV_Z = 1, 2, 3, 4

# One HV_B pad, 1.2 mm tall, between the two lattice rows the corridor nets
# take (y = 10.0 and y = 8.0): each row passes it at 0.3 mm edge to edge --
# legal at the net's own 0.2 mm, short of the pad's 0.4 mm class.
_HV_PAD_Y = 9.0
_HV_PAD_H = 1.2


def _board(*, voltage_map: bool) -> Autorouter:
    rules = DesignRules(
        trace_width=TRACE_W,
        trace_clearance=BASE,
        via_clearance=BASE,
        grid_resolution=0.05,
        # As the board loader records it: no designer-authored floors, so
        # router classes do not grow the search's pad masks (#6449).
        designer_clearance_floors={},
    )
    if voltage_map:
        rules.pairwise_clearance = build_pairwise_clearance_table(
            {"HV_B": 300.0, "HV_C": 300.0, "LV_Z": 0.0}, dru=BASE
        )
    class_map = {
        "SIG": NetClassRouting(name="Digital", trace_width=TRACE_W, clearance=BASE),
        "HV_B": NetClassRouting(name="HV", trace_width=TRACE_W, clearance=HV_CLASS),
        # An HV net in the 0.2 mm class: a 0.4 mm-class HV net's own clearance
        # already keeps the pad in search, so only a narrower class reaches
        # the backstop (calibrated on main).
        "HV_C": NetClassRouting(name="Digital", trace_width=TRACE_W, clearance=BASE),
        "LV_Z": NetClassRouting(name="Digital", trace_width=TRACE_W, clearance=BASE),
    }
    router = Autorouter(
        30,
        30,
        strategy="lattice",
        rules=rules,
        net_class_map=class_map,
        layer_stack=LayerStack.two_layer(),
    )

    def pad(net: int, name: str, x: float, y: float, **kw: float) -> dict:
        return {
            "width": 0.6,
            "height": 0.6,
            "net": net,
            "net_name": name,
            "number": "1",
            "x": x,
            "y": y,
            **kw,
        }

    router.add_component("R1", [pad(SIG, "SIG", 5.0, 10.0)])
    router.add_component("R2", [pad(SIG, "SIG", 25.0, 10.0)])
    router.add_component("J1", [pad(HV_B, "HV_B", 15.0, _HV_PAD_Y, height=_HV_PAD_H)])
    router.add_component("R3", [pad(HV_C, "HV_C", 5.0, 8.0)])
    router.add_component("R4", [pad(HV_C, "HV_C", 25.0, 8.0)])
    # The unrelated 0 V net, > 10 mm from the corridor.
    router.add_component("R5", [pad(LV_Z, "LV_Z", 2.0, 28.0)])
    router.add_component("R6", [pad(LV_Z, "LV_Z", 8.0, 28.0)])
    router.net_names.update({SIG: "SIG", HV_B: "HV_B", HV_C: "HV_C", LV_Z: "LV_Z"})
    # The CLI installs the router classes as foreign-pair floors (#6427),
    # which is what the backstop holds every pair to.
    refresh_router_class_floors(router)
    return router


def _gap_to_hv_pad(router: Autorouter, net: int) -> float:
    pad = router.pads[("J1", "1")]
    return min(
        copper_gap(segment_shape(seg), pad_shape(pad))
        for route in router.routes
        if route.net == net
        for seg in route.segments
        if seg.layer == pad.layer
    )


@pytest.mark.parametrize("voltage_map", [True, False], ids=["voltage-map", "no-voltage-map"])
def test_backstop_demotes_nothing_on_the_class_pad_corridor(voltage_map: bool) -> None:
    router = _board(voltage_map=voltage_map)
    router.route_all(suppress_no_timeout_warning=True)
    pf = router._lattice_pathfinder
    assert (pf._pairwise is not None) is voltage_map
    routed = {route.net for route in router.routes}
    assert {SIG, HV_C, LV_Z} <= routed, pf.failure_reasons

    demoted = router.revalidate_committed_copper_or_demote()

    assert demoted == []
    assert router.last_floor_demoted_nets == []
    for net in (SIG, HV_C):
        assert _gap_to_hv_pad(router, net) >= HV_CLASS - EPS


def test_voltage_map_routes_like_the_scalar_board() -> None:
    """The projection maps no corridor pair, so the copper is the same."""

    def copper(router: Autorouter) -> list:
        router.route_all(suppress_no_timeout_warning=True)
        return sorted(
            (r.net, round(s.x1, 6), round(s.y1, 6), round(s.x2, 6), round(s.y2, 6))
            for r in router.routes
            for s in r.segments
        )

    assert copper(_board(voltage_map=True)) == copper(_board(voltage_map=False))


# ---------------------------------------------------------------------------
# Growth (#6489) composes with the pairwise keep-out by ``max``, not a sum.
# ---------------------------------------------------------------------------

_HALF = TRACE_W / 2.0
_AGENT = _HALF + BASE
_REQ = 1.0
_GROW = 0.2  # a pad grown to a 0.4 mm class over the 0.2 mm base


def _model(*, composes: bool) -> LatticeObstacleModel:
    pad = Pad(
        x=20.0,
        y=20.0,
        width=1.0,
        height=1.0,
        net=HV_B,
        net_name="HV_B",
        layer=Layer.F_CU,
        ref="J1",
        pin="1",
    )
    model = LatticeObstacleModel(
        OctilinearLattice((0.0, 0.0, 40.0, 40.0), [], coarse=3.2),
        [pad],
        [(0,)],
        2,
        agent_radius=_AGENT,
        pad_extra=[_GROW],
    )
    model.pairwise_composes_pad_extra = composes
    return model


def test_grown_pad_composes_with_the_pairwise_requirement() -> None:
    pw = LatticePairwise(
        required_by_pair={(HV_B, LV_Z): _REQ}, zones=(), max_by_net={LV_Z: _REQ, HV_B: _REQ}
    )
    # The pair keep-out reaches pad half + own half + requirement from the
    # pad centre.  Just outside it:
    y = 20.0 + 0.5 + _HALF + _REQ + 0.05
    # Composed (voltage map): legal -- the pad's grown floor (0.4 mm) is far
    # below the 1.0 mm pair requirement, and the scalar path still holds it.
    assert not _model(composes=True).pairwise_pad_blocked(
        (0.0, y), (40.0, y), 0, LV_Z, _HALF, 0.0, pw
    )
    # Uncomposed (the pre-#7880 inflation every other board keeps): the
    # growth is added on top of the requirement.
    assert _model(composes=False).pairwise_pad_blocked((0.0, y), (40.0, y), 0, LV_Z, _HALF, 0.0, pw)
    # Inside the requirement both block.
    y_in = 20.0 + 0.5 + _HALF + _REQ - 0.05
    for composes in (True, False):
        assert _model(composes=composes).pairwise_pad_blocked(
            (0.0, y_in), (40.0, y_in), 0, LV_Z, _HALF, 0.0, pw
        )


def test_pathfinder_composes_only_under_a_real_projection() -> None:
    router = _board(voltage_map=True)
    router.route_all(suppress_no_timeout_warning=True)
    pf = router._lattice_pathfinder
    assert pf.obstacles.pairwise_composes_pad_extra is True
    pf.set_pairwise(None)
    assert pf.obstacles.pairwise_composes_pad_extra is False


# ---------------------------------------------------------------------------
# ``_strict_pad_pairwise`` merges into the HV projection (#6703 path).
# ---------------------------------------------------------------------------


def test_strict_pad_pairwise_merges_into_the_hv_projection() -> None:
    from types import SimpleNamespace

    router = _board(voltage_map=True)
    router.route_all(suppress_no_timeout_warning=True)
    pf = router._lattice_pathfinder
    active = pf._pairwise
    assert active is not None
    # A fallback "pair" whose legs are the 0.2 mm-class SIG and HV_C nets.
    sig_pad = router.pads[("R1", "1")]
    hvc_pad = router.pads[("R3", "1")]
    pc = SimpleNamespace(net_class=None)
    items = {"P": (None, sig_pad), "N": (None, hvc_pad)}

    strict, floor = pf._strict_pad_pairwise(pc, items)

    assert strict is not None
    # The class keep-out of the 0.4 mm-class HV_B pad, for both legs ...
    assert strict.required(SIG, HV_B) == pytest.approx(HV_CLASS)
    assert strict.required(HV_C, HV_B) == pytest.approx(HV_CLASS)
    # ... merged with -- never replacing -- every HV pair and attach zone.
    for (a, b), req in active.required_by_pair.items():
        assert strict.required(a, b) >= req
    assert strict.zones == active.zones
    assert floor == pytest.approx(HV_CLASS)
