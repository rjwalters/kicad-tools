"""Via parity for a lattice pair's extra-pad attachments (issue #7093).

An engaged pair's extra same-net pads (a USB-C receptacle's second A/B row)
are attached by single-ended connections.  Nothing kept P and N on the same
layer changes there, so a pair whose coupled run is 2 / 2 could still commit
2 / 4 vias (board 06rf ``USB2_D``).  :meth:`LatticePathfinder._extra_parity_pass`
now re-searches one extra under the other's layer changes once the board is
finished, and keeps it only when the two nets' via totals come out equal.

The copy of an ``a -> b -> a`` extra uses the new *crossing*
:class:`LayerMirror`: an exact mirror over ``{a, b}`` comes back planar on
``a`` whenever ``a`` has a way through, so it can never add the partner's
layer changes.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.pathfinder import (
    LatticePathfinder,
    LayerMirror,
    _crossing_mirror,
    _layer_sequence,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route
from kicad_tools.router.rules import DesignRules, NetClassRouting

_RULES = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
_HS = NetClassRouting(
    name="HS",
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.1,
    coupled_routing=True,
)
_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 12.0), (0.0, 12.0)]
_F, _B = 0, 1


def _pad(x: float, y: float, net: int, name: str, ref: str, **kw) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=kw.get("width", 0.5),
        height=kw.get("height", 0.5),
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref=ref,
        pin=kw.get("pin", "1"),
    )


# The extras: P along y = 2, N along y = 8, both F.Cu SMD pads 12 mm apart.
_P_A = _pad(3.0, 2.0, 1, "D+", "J1", pin="A6")
_P_B = _pad(15.0, 2.0, 1, "D+", "J1", pin="B6")
_N_A = _pad(3.0, 8.0, 2, "D-", "J1", pin="A7")
_N_B = _pad(15.0, 8.0, 2, "D-", "J1", pin="B7")
#: An F.Cu wall from just above P's row to the top edge: with P's extra laid
#: first, N can only pass the wall by dropping through B.Cu (2 vias).
_N_WALL = _pad(9.0, 7.3, 9, "WALL", "W1", width=0.6, height=9.4)

_P_EXTRA = ((1, 0), _P_A, _P_B, _HS)
_N_EXTRA = ((2, 0), _N_A, _N_B, _HS)


def _pf(*extra_pads: Pad) -> LatticePathfinder:
    return LatticePathfinder(
        _OUTLINE,
        [_P_A, _P_B, _N_A, _N_B, *extra_pads],
        _RULES,
        LayerStack.two_layer(),
    )


def _pc() -> CoupledConnection:
    return CoupledConnection(
        key=("pair", 1, 2, 0),
        pair_name="D",
        pad_p_a=_P_A,
        pad_n_a=_N_A,
        pad_p_b=_P_B,
        pad_n_b=_N_B,
        net_class=_HS,
        pitch=_HS.trace_width + _HS.effective_intra_pair_clearance(),
    )


def _route(pf: LatticePathfinder, item, *under: Route) -> Route:
    """Search ``item`` alone, against ``under`` (already-laid copper)."""
    committed = pf._fresh_committed()
    for route in under:
        pf._commit_route_copper(route, _HS, committed)
    res, why = pf._search_single(
        item,
        committed=committed,
        history={},
        present=0.0,
        escape_plan={},
    )
    assert res is not None, why
    return res.route


def _vias(routes: dict[object, Route]) -> tuple[int, int]:
    p = sum(len(r.vias) for r in routes.values() if r.net == 1)
    n = sum(len(r.vias) for r in routes.values() if r.net == 2)
    return p, n


def test_crossing_mirror_is_only_built_for_a_leg_that_leaves_and_returns() -> None:
    assert _crossing_mirror(_F, _F, frozenset({_F, _B})) == LayerMirror(
        _F, _F, frozenset({_B}), False, crossing=True
    )
    # Planar (nothing to cross on) and open-ended (a -> b) legs get none.
    assert _crossing_mirror(_F, _F, frozenset({_F})) is None
    assert _crossing_mirror(_F, _B, frozenset({_F, _B})) is None


def test_exact_mirror_comes_back_planar_but_the_crossing_mirror_changes_layer() -> None:
    # Open board: nothing forces a via, so the exact mirror of an F -> B -> F
    # partner is a planar F.Cu route -- the case it can never repair.
    pf = _pf()
    exact = LayerMirror(_F, _F, frozenset({_F, _B}), False)
    res, why = pf._search_single(
        _P_EXTRA,
        committed=pf._fresh_committed(),
        history={},
        present=0.0,
        escape_plan={},
        mirror=exact,
    )
    assert res is not None, why
    assert len(res.route.vias) == 0
    crossing = _crossing_mirror(_F, _F, frozenset({_F, _B}))
    res, why = pf._search_single(
        _P_EXTRA,
        committed=pf._fresh_committed(),
        history={},
        present=0.0,
        escape_plan={},
        mirror=crossing,
    )
    assert res is not None, why
    assert len(res.route.vias) == 2
    assert _layer_sequence(res) == (_F, _B, _F)


def test_planar_extra_takes_its_partners_layer_changes() -> None:
    # N's extra is walled off on F.Cu and drops through B.Cu (2 vias); P's
    # extra has a planar way and took it (0 vias): the pair is 0 / 2.
    pf = _pf(_N_WALL)
    p_route = _route(pf, _P_EXTRA)
    n_route = _route(pf, _N_EXTRA, p_route)
    assert (len(p_route.vias), len(n_route.vias)) == (0, 2)
    routes: dict[object, Route] = {(1, 0): p_route, (2, 0): n_route}

    pf._extra_parity_pass(routes, [_P_EXTRA, _N_EXTRA], [_pc()])

    assert _vias(routes) == (2, 2)
    assert routes[(2, 0)] is n_route  # the partner's copper did not move
    assert routes[(1, 0)] is not p_route
    note = pf.extra_parity_log[(1, 2)]
    assert "0/2 -> 2/2" in note and "J1.B6->J1.A6" in note


def test_extra_routed_round_a_vanished_obstacle_goes_back_planar() -> None:
    # N's extra was laid (2 vias) while a wall stood in its way; the finished
    # board no longer has it, so N copies P's planar extra: 0 / 0.
    pf = _pf()
    p_route = _route(pf, _P_EXTRA)
    n_route = _route(_pf(_N_WALL), _N_EXTRA, p_route)
    assert len(n_route.vias) == 2
    routes: dict[object, Route] = {(1, 0): p_route, (2, 0): n_route}

    pf._extra_parity_pass(routes, [_P_EXTRA, _N_EXTRA], [_pc()])

    assert _vias(routes) == (0, 0)
    assert routes[(1, 0)] is p_route
    assert routes[(2, 0)] is not n_route


def test_pass_never_touches_an_equal_pair_or_runs_when_disabled() -> None:
    pf = _pf(_N_WALL)
    p_route = _route(pf, _P_EXTRA)
    n_route = _route(pf, _N_EXTRA, p_route)

    equal = {(1, 0): p_route, (2, 0): p_route.__class__(net=2, net_name="D-")}
    equal[(2, 0)].segments.extend(n_route.segments)  # same copper, no vias
    before = dict(equal)
    pf._extra_parity_pass(equal, [_P_EXTRA, _N_EXTRA], [_pc()])
    assert equal == before and not pf.extra_parity_log

    pf.extra_parity = False
    routes: dict[object, Route] = {(1, 0): p_route, (2, 0): n_route}
    pf._extra_parity_pass(routes, [_P_EXTRA, _N_EXTRA], [_pc()])
    assert routes[(1, 0)] is p_route and routes[(2, 0)] is n_route
    assert not pf.extra_parity_log


def test_no_fitting_extra_keeps_the_copper_and_says_so() -> None:
    # A foreign B.Cu wall across the whole board: P cannot cross under it,
    # and N has no planar way past its F.Cu wall either.
    pf = _pf(_N_WALL)
    p_route = _route(pf, _P_EXTRA)
    n_route = _route(pf, _N_EXTRA, p_route)
    routes: dict[object, Route] = {(1, 0): p_route, (2, 0): n_route}
    original = pf._fresh_committed

    def fresh():
        committed = original()
        committed.add_run(_B, [(9.0, 0.3), (9.0, 11.7)], 8, 0.3, 0.15)
        return committed

    pf._fresh_committed = fresh  # type: ignore[method-assign]
    pf._extra_parity_pass(routes, [_P_EXTRA, _N_EXTRA], [_pc()])

    assert routes[(1, 0)] is p_route and routes[(2, 0)] is n_route
    assert "0/2" in pf.extra_parity_log[(1, 2)]
    assert pf.extra_parity_log[(1, 2)].endswith("kept")


def test_a_passed_deadline_keeps_the_copper_and_says_so() -> None:
    pf = _pf(_N_WALL)
    p_route = _route(pf, _P_EXTRA)
    n_route = _route(pf, _N_EXTRA, p_route)
    routes: dict[object, Route] = {(1, 0): p_route, (2, 0): n_route}

    pf._extra_parity_pass(routes, [_P_EXTRA, _N_EXTRA], [_pc()], deadline=0.0)

    assert routes[(1, 0)] is p_route and routes[(2, 0)] is n_route
    note = pf.extra_parity_log[(1, 2)]
    assert "0/2" in note and "deadline reached" in note and note.endswith("kept")


# -- end to end: the pass fires inside real lattice routing -------------------


def _board_pad(pin: str, x: float, y: float, net: int, name: str, **kw) -> dict:
    return {
        "number": pin,
        "x": x,
        "y": y,
        "width": kw.get("width", 0.5),
        "height": kw.get("height", 0.5),
        "net": net,
        "net_name": name,
    }


def _usb_board() -> Autorouter:
    """A coupled U1 -> U2 run plus one extra J1 pad per net, on two layers.

    The pair's main run (y = 6.5 / 7.5) is planar on F.Cu.  Each net has one
    extra pad on J1, attached to U2 by a single-ended connection: D+'s goes
    north over open board, D-'s goes south across ``W1``, a foreign F.Cu bar
    spanning the whole board width.  Negotiation therefore leaves D+'s extra
    planar and drops D-'s through B.Cu: 0 / 2 vias.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=30.0,
        height=14.0,
        rules=rules,
        net_class_map={"USB_D+": _HS, "USB_D-": _HS},
        strategy="lattice",
    )
    router.add_component(
        "U1", [_board_pad("1", 3.0, 6.5, 1, "USB_D+"), _board_pad("2", 3.0, 7.5, 2, "USB_D-")]
    )
    router.add_component(
        "U2", [_board_pad("1", 20.0, 6.5, 1, "USB_D+"), _board_pad("2", 20.0, 7.5, 2, "USB_D-")]
    )
    router.add_component(
        "J1", [_board_pad("A6", 20.0, 1.5, 1, "USB_D+"), _board_pad("A7", 20.0, 12.5, 2, "USB_D-")]
    )
    router.add_component("W1", [_board_pad("1", 15.0, 10.0, 9, "WALL", width=30.0, height=0.6)])
    return router


def _route_board(extra_parity: bool) -> tuple[Autorouter, LatticePathfinder]:
    router = _usb_board()
    pf = router._ensure_lattice_pathfinder()
    pf.extra_parity = extra_parity
    router.route_all_with_diffpairs(
        DifferentialPairConfig(enabled=True),
        non_diffpair_strategy=lambda: router.route_all(timeout=120),
    )
    return router, pf


def _committed_vias(router: Autorouter) -> tuple[int, int]:
    return tuple(  # type: ignore[return-value]
        sum(len(r.vias) for r in router.routes if r.net == net) for net in (1, 2)
    )


@pytest.mark.timeout(300)
def test_routing_a_board_runs_the_pass_and_commits_equal_vias(capsys) -> None:
    router, pf = _route_board(extra_parity=True)
    out = capsys.readouterr().out

    assert router._lattice_pair_outcomes == {"USB_D": "coupled"}
    for net in (1, 2):
        assert sum(1 for r in router.routes if r.net == net and r.segments) == 2
    # The emitted copper: D+'s extra took D-'s F -> B -> F hop.
    assert _committed_vias(router) == (2, 2)
    note = pf.extra_parity_log[(1, 2)]
    assert "vias 0/2 -> 2/2" in note and "J1.A6->U2.1" in note
    assert f"Diff-pair extra parity (lattice) {note}" in out


@pytest.mark.timeout(300)
def test_routing_the_same_board_with_the_pass_off_leaves_the_pair_unequal(capsys) -> None:
    router, pf = _route_board(extra_parity=False)
    out = capsys.readouterr().out

    assert router._lattice_pair_outcomes == {"USB_D": "coupled"}
    assert _committed_vias(router) == (0, 2)
    assert not pf.extra_parity_log
    assert "Diff-pair extra parity (lattice)" not in out


# -- joint re-route: two extras that cross in an X ----------------------------


def _x_board() -> Autorouter:
    """A USB-C-style X: each net's extra crosses the other's under J1.

    The coupled U1 -> J1 run lands on the A row (A6 = D+, A7 = D-).  The
    extras join each A pad to the B pad of the same net one row down, and
    the B row is swapped (B6 = D+ under A7, B7 = D- under A6), so the two
    extras cross like board 06rf's ``USB2_D`` J1 extras.  ``W1``, a foreign
    F.Cu bar just right of B6, shuts the planar way round B6.

    Negotiation lays D+'s extra straight across, so D-'s drops through B.Cu
    (0 / 2 vias).  Neither extra can be fixed alone: D+ copying D-'s
    F -> B -> F hop comes back planar (the pads' launch radii touch), and
    D- has no planar way past D+.  Re-routed together, D- goes straight and
    D+ goes round B7: 0 / 0.
    """
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    router = Autorouter(
        width=30.0,
        height=14.0,
        rules=rules,
        net_class_map={"USB_D+": _HS, "USB_D-": _HS},
        strategy="lattice",
    )
    router.add_component(
        "U1", [_board_pad("1", 3.0, 6.5, 1, "USB_D+"), _board_pad("2", 3.0, 7.5, 2, "USB_D-")]
    )
    usb = {"width": 0.3, "height": 0.8}
    router.add_component(
        "J1",
        [
            _board_pad("A6", 20.0, 6.0, 1, "USB_D+", **usb),
            _board_pad("A7", 21.0, 6.0, 2, "USB_D-", **usb),
            _board_pad("B6", 21.0, 8.0, 1, "USB_D+", **usb),
            _board_pad("B7", 20.0, 8.0, 2, "USB_D-", **usb),
        ],
    )
    router.add_component("W1", [_board_pad("1", 21.8, 7.0, 9, "WALL", width=0.4, height=14.0)])
    return router


def _route_x_board(extra_parity: bool) -> tuple[Autorouter, LatticePathfinder]:
    router = _x_board()
    pf = router._ensure_lattice_pathfinder()
    pf.extra_parity = extra_parity
    router.route_all_with_diffpairs(
        DifferentialPairConfig(enabled=True),
        non_diffpair_strategy=lambda: router.route_all(timeout=120),
    )
    return router, pf


def _extra_lengths(router: Autorouter, pf: LatticePathfinder) -> dict[int, float]:
    """Pair-length-measure length of each net's J1 A-row -> B-row extra."""
    measure = pf.pair_length_measure
    assert measure is not None
    out: dict[int, float] = {}
    for route in router.routes:
        ends = [(s.x1, s.y1) for s in route.segments] + [(s.x2, s.y2) for s in route.segments]
        if route.segments and all(x > 19.0 for x, _y in ends):
            assert route.net not in out
            out[route.net] = measure(route)
    assert set(out) == {1, 2}
    return out


def _net_skew(router: Autorouter, pf: LatticePathfinder) -> float:
    measure = pf.pair_length_measure
    assert measure is not None
    p = sum(measure(r) for r in router.routes if r.net == 1)
    n = sum(measure(r) for r in router.routes if r.net == 2)
    return abs(p - n)


@pytest.mark.timeout(300)
def test_crossing_extras_are_re_routed_jointly_to_equal_vias(capsys) -> None:
    from kicad_tools.router.io import validate_routes

    off, off_pf = _route_x_board(extra_parity=False)
    capsys.readouterr()
    router, pf = _route_x_board(extra_parity=True)
    out = capsys.readouterr().out

    assert router._lattice_pair_outcomes == {"USB_D": "coupled"}
    assert _committed_vias(off) == (0, 2)
    # Both extras moved, and the pair's via totals now match.
    assert _committed_vias(router) == (0, 0)
    note = pf.extra_parity_log[(1, 2)]
    assert "vias 0/2 -> 0/0" in note and "re-routed jointly" in note
    assert "J1.B6->J1.A6" in note and "J1.B7->J1.A7" in note
    assert f"Diff-pair extra parity (lattice) {note}" in out
    # The router's own clearance check passes on the committed copper.
    assert validate_routes(router) == []
    # No new extra is longer than the longer original + 0.5 mm, and the
    # pair's skew did not get worse.
    before, after = _extra_lengths(off, off_pf), _extra_lengths(router, pf)
    bound = max(before.values()) + 0.5
    assert all(length <= bound + 1e-9 for length in after.values())
    assert _net_skew(router, pf) <= _net_skew(off, off_pf) + 1e-9


@pytest.mark.timeout(300)
def test_crossing_extras_stay_unequal_with_the_pass_off(capsys) -> None:
    router, pf = _route_x_board(extra_parity=False)
    out = capsys.readouterr().out

    assert router._lattice_pair_outcomes == {"USB_D": "coupled"}
    assert _committed_vias(router) == (0, 2)
    assert not pf.extra_parity_log
    assert "Diff-pair extra parity (lattice)" not in out


@pytest.mark.timeout(300)
def test_crossing_extras_cannot_be_fixed_one_at_a_time(monkeypatch) -> None:
    # Without the joint step the single-extra search finds nothing on this
    # board: the pass logs the pair as kept at 0 / 2.
    monkeypatch.setattr(
        LatticePathfinder, "_joint_extra_parity", lambda self, *a, **k: (None, False)
    )
    router, pf = _route_x_board(extra_parity=True)

    assert _committed_vias(router) == (0, 2)
    note = pf.extra_parity_log[(1, 2)]
    assert "0/2" in note and "re-routed jointly with its partner's; kept" in note
