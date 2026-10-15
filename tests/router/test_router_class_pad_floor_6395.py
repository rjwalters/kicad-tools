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
from kicad_tools.router.lattice.class_pad_repair import (
    PAIR_LENGTH_TOLERANCE_MM,
    repair_class_pad_pairs,
)
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

    Issue #6718: ``validate_routes`` measures pads with the same exact
    clearance kernel the repair uses, so the repair no longer carries a
    second, rectangle-based "strict" mode to keep the pre-save check quiet.
    Both see the real 0.21 mm gap, which clears the 0.2 mm Power class.
    """
    from kicad_tools.router import io as router_io
    from kicad_tools.router.lattice import class_pad_repair

    assert not hasattr(class_pad_repair, "_pad_gap")

    pad = Pad(x=0.0, y=0.0, width=1.7, height=1.7, net=0, net_name="GND", ref="J4", pin="6")
    pad.shape = "circle"
    # A 45-degree track whose edge sits 0.21 mm off the circle.
    off = (0.85 + 0.21 + TRACE_W / 2) / 2**0.5
    seg = _seg(off - 1.0, off + 1.0, off + 1.0, off - 1.0)
    kernel = copper_gap(segment_shape(seg), pad_shape(pad))
    assert kernel == pytest.approx(0.21, abs=1e-3)
    assert kernel > POWER
    # validate_routes no longer models the pad as its bounding rectangle.
    assert not hasattr(router_io, "_segment_to_aabb_distance")


# ---------------------------------------------------------------------------
# Issue #6437: pair nets move only within P5's length budget; via shortfalls
# reported; HV skip logged.
# ---------------------------------------------------------------------------


def _coords(routes: dict) -> list:
    return [(s.x1, s.y1, s.x2, s.y2) for r in routes.values() for s in r.segments]


def _net_length(routes: dict, net: int) -> float:
    return sum(
        ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5
        for r in routes.values()
        if r.net == net
        for s in r.segments
    )


def test_pair_net_move_within_the_length_budget_is_kept() -> None:
    """An extra-pad stub of a pair net (keyed ``(net, seq)``, repairable) may
    move: the straight-run shift changes its length by ~0.0002 mm, far inside
    P5's 0.01 mm skew threshold (board 03 USB_D-: +0.009 mm)."""
    pf, routes = _repair_case()
    before = _net_length(routes, 1)
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes), pair_nets=frozenset({1}))
    assert (stats.found, stats.repaired, stats.remaining, stats.pair_skipped) == (1, 1, 0, 0)
    assert abs(_net_length(routes, 1) - before) <= PAIR_LENGTH_TOLERANCE_MM
    mid = routes[(1, 0)].segments[1]
    assert copper_gap(segment_shape(mid), pad_shape(pf.pads[2])) >= POWER - EPS


def _diagonal_case() -> tuple[LatticePathfinder, dict]:
    """The middle leg sits between two 45-degree legs, so shifting it off the
    GND pad by ~0.027 mm stretches the net by ~0.038 mm."""
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    term_a = Pad(x=5.0, y=9.0, width=0.6, height=0.6, net=1, net_name="SIG", ref="R1", pin="1")
    term_b = Pad(x=15.0, y=9.0, width=0.6, height=0.6, net=1, net_name="SIG", ref="R2", pin="1")
    gnd_y = 10.0 - TRACE_W / 2 - 0.175 - 0.3
    gnd = Pad(x=10.0, y=gnd_y, width=0.6, height=0.6, net=0, net_name="GND", ref="C1", pin="1")
    pf = LatticePathfinder(outline, [term_a, term_b, gnd], _rules())
    pf.set_pad_class_clearances([BASE, BASE, POWER])
    run = [_seg(5.0, 9.0, 6.0, 10.0), _seg(6.0, 10.0, 14.0, 10.0), _seg(14.0, 10.0, 15.0, 9.0)]
    return pf, {(1, 0): Route(1, "SIG", segments=run)}


def test_diagonal_case_is_repaired_for_an_ordinary_net() -> None:
    pf, routes = _diagonal_case()
    before = _net_length(routes, 1)
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes))
    assert (stats.found, stats.repaired, stats.remaining) == (1, 1, 0)
    assert _net_length(routes, 1) - before > PAIR_LENGTH_TOLERANCE_MM


def test_pair_net_move_beyond_the_length_budget_is_refused() -> None:
    """The same move on a pair net would shift its skew past 0.01 mm."""
    pf, routes = _diagonal_case()
    before = _coords(routes)
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes), pair_nets=frozenset({1}))
    assert _coords(routes) == before
    assert (stats.found, stats.repaired, stats.remaining, stats.pair_skipped) == (1, 0, 1, 1)


def test_pair_leg_never_moves() -> None:
    """A ``(pc.key, "P")`` leg (coupled or #6373 fallback) is not repairable:
    it stays put and its shortfall is counted, not hidden."""
    pf, routes = _repair_case()
    leg = {(("pair", 1, 3), "P"): routes[(1, 0)]}
    before = _coords(leg)
    stats = repair_class_pad_pairs(
        pf, leg, dict.fromkeys(leg), repairable=set(), pair_nets=frozenset({1, 3})
    )
    assert _coords(leg) == before
    assert (stats.repaired, stats.pair_skipped) == (0, 1)


def _opposite_sign_pair_case() -> tuple[LatticePathfinder, dict]:
    """Two nets of one pair, each needing a move that is inside the budget alone.

    Net 1's end legs rise toward the middle leg (shifting it up LENGTHENS the
    net by ~0.0097 mm); net 3's fall toward it (the same shift SHORTENS it by
    ~0.0083 mm).  Both moves together would change the pair's skew by ~0.018 mm.
    """
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    pads = []
    run_ends = {1: (9.83, 10.0), 3: (15.17, 15.0)}
    for net, (end_y, _mid_y) in run_ends.items():
        for x, ref in ((5.0, "R1"), (15.0, "R2")):
            pads.append(
                Pad(x=x, y=end_y, width=0.6, height=0.6, net=net, net_name="P", ref=ref, pin="1")
            )
    for mid_y, ref in ((10.0, "C1"), (15.0, "C2")):
        gnd_y = mid_y - TRACE_W / 2 - 0.175 - 0.3
        pads.append(
            Pad(x=10.0, y=gnd_y, width=0.6, height=0.6, net=0, net_name="GND", ref=ref, pin="1")
        )
    pf = LatticePathfinder(outline, pads, _rules())
    pf.set_pad_class_clearances([BASE] * 4 + [POWER, POWER])
    routes = {}
    for net, (end_y, mid_y) in run_ends.items():
        run = [
            _seg(5.0, end_y, 6.0, mid_y, net=net),
            _seg(6.0, mid_y, 14.0, mid_y, net=net),
            _seg(14.0, mid_y, 15.0, end_y, net=net),
        ]
        routes[(net, 0)] = Route(net, "P" if net == 1 else "N", segments=run)
    return pf, routes


def test_opposite_sign_moves_on_both_pair_nets_are_budgeted_as_skew() -> None:
    """Each net's move is within 0.01 mm alone, but they pull in opposite
    directions: the second is refused so the pair's skew stays in budget."""
    pf, routes = _opposite_sign_pair_case()
    before = {n: _net_length(routes, n) for n in (1, 3)}
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes), pair_nets={1: 3, 3: 1})
    skew = abs((_net_length(routes, 1) - before[1]) - (_net_length(routes, 3) - before[3]))
    assert skew <= PAIR_LENGTH_TOLERANCE_MM
    assert (stats.found, stats.repaired, stats.remaining, stats.pair_skipped) == (2, 1, 1, 1)


def test_opposite_sign_moves_are_both_kept_for_unpaired_nets() -> None:
    """Control: the same two moves are fine for ordinary nets, so the refusal
    above comes from the pair's skew budget, not from the geometry."""
    pf, routes = _opposite_sign_pair_case()
    stats = repair_class_pad_pairs(pf, routes, dict.fromkeys(routes))
    assert (stats.found, stats.repaired, stats.remaining, stats.pair_skipped) == (2, 2, 0, 0)


def test_pair_tolerance_cites_p5() -> None:
    assert PAIR_LENGTH_TOLERANCE_MM == 0.01


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


def test_declined_pair_routed_single_ended_is_held_to_the_budget(capsys, monkeypatch) -> None:
    """A detected-but-declined pair goes entirely through the ordinary star, so
    its copper is repairable -- but under the pair length budget.  Here the run
    meets 45-degree legs at both ends: the ~0.027 mm shift would shorten the
    net by ~0.038 mm, so the move is refused, left in place and reported."""
    # Isolate the repair's budget: #6489's re-negotiation would route round the pad.
    monkeypatch.setattr("kicad_tools.router.lattice.pathfinder._MAX_PAD_GROWTH_ROUNDS", 0)
    router = _pair_board()
    routes = router.route_net(1)
    assert routes and routes[0].segments
    assert router._lattice_pair_net_ids == {1: 3, 3: 1}
    stats = router._lattice_pathfinder.class_pad_repair_stats
    assert (stats.found, stats.repaired, stats.remaining, stats.pair_skipped) == (1, 0, 1, 1)
    assert _min_gap_to_cap(router, routes) < POWER - EPS
    out = capsys.readouterr().out
    assert "1 on diff-pair nets left in place (skew budget 0.01 mm)" in out


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


# ---------------------------------------------------------------------------
# Issue #6489: an unrepairable slot becomes a search constraint.
# ---------------------------------------------------------------------------

_SLOT_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


def _slot_pads(extra: list[Pad] | None = None) -> list[Pad]:
    """Two Power-class GND bars whose gap (0.55 mm) is below ``0.2 + 2 x 0.2``."""
    wall = {"width": 8.0, "height": 0.6, "net": 0, "net_name": "GND", "ref": "J1"}
    return [
        Pad(x=10.0, y=10.0 - 0.575, pin="1", **wall),
        Pad(x=10.0, y=10.0 + 0.575, pin="2", **wall),
        *(extra or []),
    ]


def _slot_pf(extra: list[Pad]) -> LatticePathfinder:
    pads = _slot_pads(extra)
    pf = LatticePathfinder(_SLOT_OUTLINE, pads, _rules(), LayerStack.two_layer())
    pf.set_pad_class_clearances([POWER, POWER] + [BASE] * len(extra))
    return pf


def _left(pf: LatticePathfinder, routes: dict) -> int:
    n = 0
    for route in routes.values():
        for seg in route.segments:
            for pad in pf.pads[:2]:
                if (
                    seg.layer == pad.layer
                    and copper_gap(segment_shape(seg), pad_shape(pad)) < POWER - EPS
                ):
                    n += 1
    return n


def test_single_ended_route_avoids_a_slot_the_repair_cannot_clear() -> None:
    a = Pad(x=2.0, y=10.0, width=0.6, height=0.6, net=1, net_name="SIG", ref="R1", pin="1")
    b = Pad(x=18.0, y=10.0, width=0.6, height=0.6, net=1, net_name="SIG", ref="R2", pin="1")
    pf = _slot_pf([a, b])
    routes, stats = pf.route_netset([((1, 0), a, b, None)])
    assert (1, 0) in routes and stats.routed == 1
    assert _left(pf, routes) == 0
    assert pf.class_pad_repair_stats is not None
    assert pf.class_pad_repair_stats.total_remaining == 0
    assert stats.lattice_builds == 2  # one targeted growth round, no more


def _slot_pair():
    from kicad_tools.router.lattice.coupled import CoupledConnection

    def pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
        return Pad(x=x, y=y, width=0.6, height=0.6, net=net, net_name=name, ref=ref, pin="1")

    # A 1 mm pitch is too wide for the coupled search here, which declines
    # (``uncoupled-fallback``); the legs then route single-ended.
    pp_a, pn_a = pad(2.0, 9.5, 1, "D+", "J2"), pad(2.0, 10.5, 2, "D-", "J2")
    pp_b, pn_b = pad(18.0, 9.5, 1, "D+", "U1"), pad(18.0, 10.5, 2, "D-", "U1")
    pf = _slot_pf([pp_a, pn_a, pp_b, pn_b])
    pc = CoupledConnection(
        key=("pair", 1, 2),
        pair_name="D",
        pad_p_a=pp_a,
        pad_n_a=pn_a,
        pad_p_b=pp_b,
        pad_n_b=pn_b,
        net_class=None,
        pitch=1.0,
    )
    return pf, pc


def test_uncoupled_fallback_pair_leg_avoids_the_slot(monkeypatch) -> None:
    # Pre-#6703 (partner-blind) legs reach the slot; the grown keep-out round
    # moves them out of it.
    def blind(self, pc, **kw):
        return self._partner_blind_legs(pc, **kw), True

    monkeypatch.setattr(LatticePathfinder, "_partner_aware_legs", blind)
    pf, pc = _slot_pair()
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    assert stats.lattice_builds == 2
    legs = [k for k in routes if isinstance(k, tuple) and k[1:] in (("P",), ("N",))]
    assert len(legs) == 2
    assert _left(pf, routes) == 0


def test_partner_aware_fallback_legs_still_clear_the_slot() -> None:
    # Issue #6703: the partner-aware flush keeps its own legs only for a
    # material skew gain (PR #6735 review); dodging a class-pad shortfall
    # alone is left to the growth round, which still clears the slot.
    pf, pc = _slot_pair()
    routes, stats = pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    assert pf.pair_outcomes[pc.key].startswith("uncoupled-fallback:")
    assert stats.lattice_builds <= 2
    legs = [k for k in routes if isinstance(k, tuple) and k[1:] in (("P",), ("N",))]
    assert len(legs) == 2
    assert _left(pf, routes) == 0


def test_no_shortfall_means_no_rebuild_and_identical_routes() -> None:
    a = Pad(x=2.0, y=3.0, width=0.6, height=0.6, net=1, net_name="SIG", ref="R1", pin="1")
    b = Pad(x=18.0, y=3.0, width=0.6, height=0.6, net=1, net_name="SIG", ref="R2", pin="1")
    pf = _slot_pf([a, b])
    routes, stats = pf.route_netset([((1, 0), a, b, None)])
    assert stats.lattice_builds == 1
    assert pf._class_pad_growth == {}
    pf2 = _slot_pf([a, b])
    routes2, _ = pf2._route_netset_once([((1, 0), a, b, None)])
    assert _coords(routes) == _coords(routes2)
