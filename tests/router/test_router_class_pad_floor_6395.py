"""A pad's own router net class is the pad side of every pair (issue #6395).

KiCad holds two foreign coppers to ``max(class(a), class(b))``.  The lattice
engine applied a connection's own class to its copper and kept routed-vs-routed
pairs symmetric, but a PAD's side came only from authored ``.kicad_pro`` floors
(#6243).  Router-derived classes never reached it, and a skipped pour net
(GND, +5V -- neutralised to net 0 by the loader) lost its ``Power`` class
entirely.  On board 05 a 0.15 mm ``Digital`` track passed 0.2 mm ``Power`` pads
at 0.175-0.19 mm: 6 findings in both DRC engines once #6390 declared the classes.

The lattice now resolves each pad's class in the router and, after negotiation,
moves an offending segment off the pad by the shortfall when every touched
segment stays clear of all other copper (``lattice/class_pad_repair.py``; its
docstring records why the search's pad masks are deliberately not grown).

The grid/C++ engine already honours the pair through its board-wide
pad-clearance floor (issue #6315); it is pinned here so both engines keep it.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.clearance_shapes import copper_gap, pad_shape, segment_shape, via_shape
from kicad_tools.router.core import Autorouter
from kicad_tools.router.lattice.class_pad_repair import repair_class_pad_pairs
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

BASE = 0.15  # board-global trace clearance (and the signal's own class)
POWER = 0.2  # the pour net's router class
TRACE_W = 0.2
EPS = 1e-6

# The lattice runs the signal along its y = 9.6 row; a GND pad centred here
# leaves that row 0.175 mm from the pad -- legal at the signal's own 0.15 mm,
# a Power-class violation (the board 05 SPEED / PWM_B / GND_SENSE case).
_TIGHT_GND_Y = 9.6 - TRACE_W / 2 - 0.175 - 0.3


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=TRACE_W,
        trace_clearance=BASE,
        via_clearance=BASE,
        grid_resolution=0.05,
    )


def _class_map() -> dict[str, NetClassRouting]:
    return {
        "SIG": NetClassRouting(name="Digital", trace_width=TRACE_W, clearance=BASE),
        "GND": NetClassRouting(name="Power", trace_width=0.5, clearance=POWER, is_pour_net=True),
    }


def _board(engine: str, *, neutral: bool = True) -> Autorouter:
    """SIG between two pads; a GND cap whose pad 1 sits 0.175 mm off its path.

    ``neutral`` models the skipped pour net (net 0 + retained ``net_name``);
    otherwise the cap's pads keep a real routing net id.
    """
    kw = {"rules": _rules(), "net_class_map": _class_map(), "layer_stack": LayerStack.two_layer()}
    if engine == "lattice":
        router = Autorouter(30, 20, strategy="lattice", **kw)
    else:
        router = Autorouter(30, 20, force_python=False, **kw)
    sig = {"width": 0.6, "height": 0.6, "net": 1, "net_name": "SIG", "number": "1"}
    router.add_component("R1", [{**sig, "x": 5.0, "y": 10.0}])
    router.add_component("R2", [{**sig, "x": 25.0, "y": 10.0}])
    gnd = {"width": 0.6, "height": 0.6, "net": 0 if neutral else 2, "net_name": "GND"}
    router.add_component(
        "C1",
        [
            {**gnd, "number": "1", "x": 15.0, "y": _TIGHT_GND_Y},
            {**gnd, "number": "2", "x": 15.0, "y": _TIGHT_GND_Y - 3.0},
        ],
    )
    router.net_names[1] = "SIG"
    if not neutral:
        router.net_names[2] = "GND"
    return router


def _min_gap_to_cap(router: Autorouter, routes) -> float:
    best = float("inf")
    pads = [p for (ref, _pin), p in router.pads.items() if ref == "C1"]
    for route in routes:
        for pad in pads:
            for seg in route.segments:
                if seg.layer == pad.layer:
                    best = min(best, copper_gap(segment_shape(seg), pad_shape(pad)))
            for via in route.vias:
                best = min(best, copper_gap(via_shape(via), pad_shape(pad)))
    return best


@pytest.mark.parametrize("neutral", [True, False], ids=["skipped-pour-pad", "routed-net-pad"])
def test_lattice_track_keeps_the_pad_class_clearance(neutral: bool) -> None:
    """The reported defect: 0.175 mm next to a 0.2 mm Power pad (pre-fix)."""
    router = _board("lattice", neutral=neutral)
    routes = router.route_net(1)
    assert routes and routes[0].segments
    assert _min_gap_to_cap(router, routes) >= POWER - EPS
    stats = router._lattice_pathfinder.class_pad_repair_stats
    assert (stats.found, stats.repaired, stats.remaining) == (1, 1, 0)


def test_cpp_grid_track_keeps_the_pad_class_clearance() -> None:
    """The grid/C++ engine holds the same pair (#6315 board-wide pad floor)."""
    from kicad_tools.router.cpp_backend import is_cpp_available

    if not is_cpp_available():
        pytest.skip("C++ router backend not built (`kct build-native`)")
    router = _board("cpp")
    routes = router.route_net(1)
    assert routes
    assert _min_gap_to_cap(router, routes) >= POWER - EPS


# ---------------------------------------------------------------------------
# Resolution: which class a pad carries.
# ---------------------------------------------------------------------------


def _pad(net: int, name: str, x: float = 5.0) -> Pad:
    return Pad(x=x, y=5.0, width=0.6, height=0.6, net=net, net_name=name, ref="U1", pin=str(x))


def test_pad_class_resolves_by_routing_net_or_retained_name() -> None:
    router = Autorouter(20, 20, rules=_rules(), net_class_map=_class_map(), strategy="lattice")
    router.net_names = {1: "SIG", 2: "GND"}
    pads = [_pad(1, "SIG"), _pad(2, "GND", 6.0), _pad(0, "GND", 7.0), _pad(0, "", 8.0)]
    # Routing id wins; a neutralised pad falls back to its retained net name;
    # an anonymous pad has no class (the board-global clearance governs).
    assert router._pad_class_clearances(pads, BASE) == [BASE, POWER, POWER, 0.0]


def test_no_class_above_the_base_installs_nothing() -> None:
    flat = {"SIG": NetClassRouting(name="Digital", trace_width=TRACE_W, clearance=BASE)}
    router = Autorouter(20, 20, rules=_rules(), net_class_map=flat, strategy="lattice")
    router.net_names = {1: "SIG"}
    assert router._pad_class_clearances([_pad(1, "SIG"), _pad(0, "GND", 6.0)], BASE) is None


def test_pathfinder_rejects_misaligned_pad_classes() -> None:
    outline = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    pf = LatticePathfinder(outline, [_pad(1, "SIG")], _rules())
    with pytest.raises(ValueError, match="align"):
        pf.set_pad_class_clearances([0.2, 0.2])


# ---------------------------------------------------------------------------
# The repair itself: moves only when everything it touches stays clear.
# ---------------------------------------------------------------------------


def _seg(x1: float, y1: float, x2: float, y2: float, net: int = 1) -> Segment:
    return Segment(x1, y1, x2, y2, TRACE_W, Layer.F_CU, net, "SIG" if net == 1 else "X")


def _repair_case(blocker_y: float | None = None) -> tuple[LatticePathfinder, dict]:
    """A 3-segment signal run whose middle leg passes a GND pad at 0.175 mm.

    ``blocker_y`` adds a foreign 0.15 mm-class track above the run.
    """
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    term_a = Pad(x=2.0, y=10.0, width=0.6, height=0.6, net=1, net_name="SIG", ref="R1", pin="1")
    term_b = Pad(x=18.0, y=10.0, width=0.6, height=0.6, net=1, net_name="SIG", ref="R2", pin="1")
    gnd_y = 10.0 - TRACE_W / 2 - 0.175 - 0.3
    gnd = Pad(x=10.0, y=gnd_y, width=0.6, height=0.6, net=0, net_name="GND", ref="C1", pin="1")
    pf = LatticePathfinder(outline, [term_a, term_b, gnd], _rules())
    pf.set_pad_class_clearances([BASE, BASE, POWER])
    run = [_seg(2.0, 10.0, 6.0, 10.0), _seg(6.0, 10.0, 14.0, 10.0), _seg(14.0, 10.0, 18.0, 10.0)]
    routes = {(1, 0): Route(1, "SIG", segments=run)}
    if blocker_y is not None:
        routes[(2, 0)] = Route(2, "X", segments=[_seg(4.0, blocker_y, 16.0, blocker_y, net=2)])
    return pf, routes


def test_repair_moves_the_leg_off_the_pad_and_keeps_it_connected() -> None:
    pf, routes = _repair_case()
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes))
    assert (stats.found, stats.repaired, stats.remaining) == (1, 1, 0)
    a, mid, b = routes[(1, 0)].segments
    assert copper_gap(segment_shape(mid), pad_shape(pf.pads[2])) >= POWER - EPS
    # Neighbours follow the moved vertices; the pad-attached ends never move.
    assert (a.x1, a.y1) == (2.0, 10.0) and (b.x2, b.y2) == (18.0, 10.0)
    assert (a.x2, a.y2) == (mid.x1, mid.y1) and (mid.x2, mid.y2) == (b.x1, b.y1)


def test_repair_refuses_a_move_into_other_copper() -> None:
    """No slack on the far side: the shortfall is left in place and counted."""
    # The foreign track's edge sits 0.16 mm above the run: any upward move of
    # the needed ~0.027 mm would break the 0.15 mm pair on that side.
    pf, routes = _repair_case(blocker_y=10.0 + TRACE_W + 0.16)
    before = [(s.x1, s.y1, s.x2, s.y2) for s in routes[(1, 0)].segments]
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes))
    assert (stats.found, stats.repaired, stats.remaining) == (1, 0, 1)
    assert [(s.x1, s.y1, s.x2, s.y2) for s in routes[(1, 0)].segments] == before


def test_repair_is_dormant_without_pad_classes() -> None:
    pf, routes = _repair_case()
    pf.set_pad_class_clearances(None)
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes))
    assert (stats.found, stats.repaired, stats.remaining) == (0, 0, 0)
    assert routes[(1, 0)].segments[1].y1 == 10.0


def test_repair_never_moves_pair_legs() -> None:
    pf, routes = _repair_case()
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes), repairable=set())
    assert (stats.found, stats.repaired) == (0, 0)
    assert routes[(1, 0)].segments[1].y1 == 10.0


def test_shortfall_is_judged_by_the_kernel_not_the_pad_rectangle() -> None:
    """A diagonal track off a round pad's corner: real DRC passes it.

    The pre-save check models the pad as its full square and reads less gap
    there, so the repair tries to satisfy that model first, but it only ever
    counts (and must clear) what the exact kernel -- and KiCad -- measure.
    """
    from kicad_tools.router.lattice.class_pad_repair import _pad_gap

    pad = Pad(x=0.0, y=0.0, width=1.7, height=1.7, net=0, net_name="GND", ref="J4", pin="6")
    pad.shape = "circle"
    # A 45-degree track whose edge sits 0.21 mm off the circle.
    off = (0.85 + 0.21 + TRACE_W / 2) / 2**0.5
    seg = _seg(off - 1.0, off + 1.0, off + 1.0, off - 1.0)
    shape = segment_shape(seg)
    kernel = _pad_gap(seg, shape, pad, strict=False)
    strict = _pad_gap(seg, shape, pad, strict=True)
    assert kernel == pytest.approx(0.21, abs=1e-3)
    assert strict < POWER < kernel


# ---------------------------------------------------------------------------
# Issue #6437: diff-pair nets are frozen; via shortfalls reported; HV skip logged.
# ---------------------------------------------------------------------------


def _coords(routes: dict) -> list:
    return [(s.x1, s.y1, s.x2, s.y2) for r in routes.values() for s in r.segments]


def test_repair_freezes_a_pair_nets_extra_pad_connection() -> None:
    """An extra-pad stub of a pair net is keyed ``(net, seq)`` and repairable,
    yet must not move: its net is frozen (board 03 USB_D skew 12.170 -> 12.179)."""
    pf, routes = _repair_case()
    before = _coords(routes)
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes), frozen_nets=frozenset({1}))
    assert _coords(routes) == before
    assert (stats.found, stats.repaired, stats.pair_skipped) == (0, 0, 1)


def test_repair_freezes_an_uncoupled_fallback_leg() -> None:
    """A ``(pc.key, "P")`` fallback leg (#6373) stays put and is counted."""
    pf, routes = _repair_case()
    leg = {(("pair", 1, 3), "P"): routes[(1, 0)]}
    before = _coords(leg)
    stats = repair_class_pad_pairs(
        pf, leg, dict.fromkeys(leg), repairable=set(), frozen_nets=frozenset({1, 3})
    )
    assert _coords(leg) == before
    assert (stats.repaired, stats.pair_skipped) == (0, 1)


def test_repair_freezes_only_the_pair_net() -> None:
    """Freezing another net leaves the ordinary single-ended repair intact."""
    pf, routes = _repair_case()
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes), frozen_nets=frozenset({7}))
    assert (stats.found, stats.repaired, stats.remaining, stats.pair_skipped) == (1, 1, 0, 0)


def _pair_board() -> Autorouter:
    """USB_D+ passes the Power-class GND pad at 0.175 mm; USB_D- runs clear.

    ``coupled_routing`` is off, so the detected pair is declined and both
    nets go entirely through the ordinary single-ended star.
    """
    cm = _class_map()
    cm["USB_D+"] = cm["SIG"]
    cm["USB_D-"] = cm["SIG"]
    router = Autorouter(
        30,
        20,
        strategy="lattice",
        rules=_rules(),
        net_class_map=cm,
        layer_stack=LayerStack.two_layer(),
    )
    pin = {"width": 0.6, "height": 0.6, "number": "1"}
    dp = {**pin, "net": 1, "net_name": "USB_D+"}
    dn = {**pin, "net": 3, "net_name": "USB_D-"}
    router.add_component("R1", [{**dp, "x": 5.0, "y": 10.0}])
    router.add_component("R2", [{**dp, "x": 25.0, "y": 10.0}])
    router.add_component("R3", [{**dn, "x": 5.0, "y": 16.0}])
    router.add_component("R4", [{**dn, "x": 25.0, "y": 16.0}])
    gnd = {"width": 0.6, "height": 0.6, "net": 0, "net_name": "GND"}
    router.add_component(
        "C1",
        [
            {**gnd, "number": "1", "x": 15.0, "y": _TIGHT_GND_Y},
            {**gnd, "number": "2", "x": 15.0, "y": _TIGHT_GND_Y - 3.0},
        ],
    )
    router.net_names[1] = "USB_D+"
    router.net_names[3] = "USB_D-"
    return router


def test_declined_pair_routed_single_ended_is_frozen(capsys) -> None:
    router = _pair_board()
    routes = router.route_net(1)
    assert routes and routes[0].segments
    assert router._lattice_pair_net_ids == frozenset({1, 3})
    stats = router._lattice_pathfinder.class_pad_repair_stats
    assert (stats.found, stats.repaired, stats.pair_skipped) == (0, 0, 1)
    # Left in place: still the 0.175 mm the search produced.
    assert _min_gap_to_cap(router, routes) < POWER - EPS
    assert "1 on diff-pair nets left in place" in capsys.readouterr().out


def _via(x: float, y: float, net: int = 1):
    from kicad_tools.router.primitives import Via

    return Via(x=x, y=y, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=net)


@pytest.mark.parametrize(("clearance", "expected"), [(0.175, 1), (0.25, 0)])
def test_via_pad_shortfall_is_reported_not_moved(clearance: float, expected: int) -> None:
    pf, routes = _repair_case()
    gnd = pf.pads[2]
    # Clear of the pad by ``clearance`` (edge to edge), below it.
    via = _via(gnd.x, gnd.y - 0.3 - clearance - 0.3)
    routes[(1, 0)].vias.append(via)
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes))
    assert (stats.via_found, stats.via_remaining) == (expected, expected)
    assert (via.x, via.y) == (gnd.x, gnd.y - 0.3 - clearance - 0.3)


def test_voltage_map_board_is_skipped_visibly() -> None:
    pf, routes = _repair_case()
    pf._pairwise = object()  # any HV projection disables the repair
    before = _coords(routes)
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes))
    assert stats.skipped == "voltage-map"
    assert (stats.found, stats.repaired) == (0, 0)
    assert _coords(routes) == before


def test_router_logs_the_voltage_map_skip(monkeypatch, capsys) -> None:
    import kicad_tools.router.lattice.class_pad_repair as cpr

    real = cpr.repair_class_pad_pairs

    def with_hv(pf, *args, **kwargs):
        saved, pf._pairwise = pf._pairwise, object()
        try:
            return real(pf, *args, **kwargs)
        finally:
            pf._pairwise = saved

    monkeypatch.setattr(cpr, "repair_class_pad_pairs", with_hv)
    router = _board("lattice")
    assert router.route_net(1)
    assert router._lattice_pathfinder.class_pad_repair_stats.skipped == "voltage-map"
    out = capsys.readouterr().out
    assert "Pad class clearance (#6395): skipped -- HV pairwise clearance" in out
