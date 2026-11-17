"""Post-pass pair-skew invariant (Issue #6447, Epic #6441 Phase 5).

The #7784 regression: the via-hairpin collapse left a DQS_P via on its own
SMT land, ``drc_verify_and_nudge``'s via-in-pad sweep slid it off, and board
07's DQS skew went from 0.119 mm to 1.291 mm with nothing noticing until
#7812.  These tests replay that shape on a synthetic pair -- real
:class:`Autorouter`, real pads, the real nudge pass -- and check that the
shared guard (:mod:`kicad_tools.router.pair_postpass_invariant`) puts the
pair back and says so.
"""

from __future__ import annotations

import pytest

from kicad_tools.router import pair_postpass_invariant as ppi
from kicad_tools.router.core import Autorouter
from kicad_tools.router.drc_nudge import drc_verify_and_nudge
from kicad_tools.router.layers import Layer
from kicad_tools.router.pcb_retune import RetuneError, _require_no_pair_skew_regression
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting

F, B = Layer.F_CU, Layer.B_CU
Y = 20.0
LIMIT = 0.1  # the class's ``kct check`` skew limit (an error)


def _class_map(limit: float = LIMIT) -> dict[str, NetClassRouting]:
    return {
        "DQS_P": NetClassRouting(name="DQS", skew_tolerance_mm=limit, diffpair_partner="DQS_N"),
        "DQS_N": NetClassRouting(name="DQS", skew_tolerance_mm=limit, diffpair_partner="DQS_P"),
    }


def _pad(number: str, x: float, y: float, net: int, name: str, size: float = 1.0) -> dict:
    return {
        "number": number,
        "x": x,
        "y": y,
        "width": size,
        "height": size,
        "net": net,
        "net_name": name,
        "layer": F,
    }


def _leg(net: int, name: str, y: float, via_x: float) -> Route:
    """Pad -> F.Cu -> via -> B.Cu down and across -> via -> F.Cu -> pad.

    The leg turns at its first via, so sliding that via off the land along
    the B.Cu run changes the leg's measured length.
    """

    def seg(x1: float, y1: float, x2: float, y2: float, layer: Layer) -> Segment:
        return Segment(x1, y1, x2, y2, 0.1, layer, net=net, net_name=name)

    def via(x: float, vy: float) -> Via:
        return Via(x, vy, 0.3, 0.6, (F, B), net=net, net_name=name)

    yb = y - 5.0
    return Route(
        net=net,
        net_name=name,
        segments=[
            seg(10.0, y, via_x, y, F),
            seg(via_x, y, via_x, yb, B),
            seg(via_x, yb, 28.0, yb, B),
            seg(28.0, yb, 30.0, yb, F),
        ],
        vias=[via(via_x, y), via(28.0, yb)],
    )


def _nudge_board(limit: float = LIMIT) -> tuple[Autorouter, Route, Route]:
    """A matched pair whose P via sits on its own 1 mm land, as #7784 left DQS_P."""
    rules = DesignRules(trace_width=0.1, trace_clearance=0.1, grid_resolution=0.1)
    router = Autorouter(width=40.0, height=40.0, rules=rules)
    router.add_component(
        "U1", [_pad("1", 10.0, Y, 1, "DQS_P"), _pad("2", 10.0, Y + 1.5, 2, "DQS_N")]
    )
    router.add_component(
        "U2", [_pad("1", 30.0, Y - 5.0, 1, "DQS_P"), _pad("2", 30.0, Y - 3.5, 2, "DQS_N")]
    )
    router.net_names.update({1: "DQS_P", 2: "DQS_N"})
    router.net_class_map = _class_map(limit)
    p = _leg(1, "DQS_P", Y, 10.3)  # drill overlaps the U1.1 land
    n = _leg(2, "DQS_N", Y + 1.5, 11.2)  # clear of U1.2: same length, nothing to move
    for route in (p, n):
        router.grid.mark_route(route)
        router.routes.append(route)
    return router, p, n


def _skew(router: Autorouter) -> float:
    pairs = ppi.guarded_pairs(router)
    assert [pair.name for pair in pairs] == ["DQS_P/DQS_N"]
    measure = ppi.checker_length_measure(router)
    assert measure is not None
    skew = ppi.measure_pair_skews(pairs, router.routes, measure)[pairs[0].nets]
    assert skew is not None
    return skew.mm


def test_unguarded_via_in_pad_sweep_breaks_the_pair(monkeypatch):
    """The reproduction: without the guard the sweep pushes skew past the limit."""
    monkeypatch.setattr(ppi.PairSkewGuard, "arm", classmethod(lambda cls, *a, **k: None))
    router, p, _n = _nudge_board()
    assert _skew(router) <= LIMIT

    result = drc_verify_and_nudge(router)

    assert result.vias_nudged == 1
    assert p.vias[0].y != pytest.approx(Y)  # the via was slid off its land
    assert _skew(router) > LIMIT + ppi.PAIR_SKEW_REGRESSION_TOL_MM


def test_nudge_guard_reverts_the_pair_and_reports_it():
    """With the guard the pair leaves the pass exactly as it entered."""
    router, p, n = _nudge_board()
    before_p, before_n = p.copy_geometry(), n.copy_geometry()
    skew_before = _skew(router)

    result = drc_verify_and_nudge(router)

    assert _skew(router) == pytest.approx(skew_before)
    assert p.segments == before_p.segments and p.vias == before_p.vias
    assert n.segments == before_n.segments and n.vias == before_n.vias
    assert len(result.pair_skew_reverts) == 1
    line = result.pair_skew_reverts[0]
    assert "DQS_P/DQS_N" in line and "newly_exceeds" in line and "drc-nudge" in line
    assert "Pair-skew guard: 1 pair(s) reverted" in result.summary()


def test_nudge_guard_leaves_a_pair_whose_change_stays_inside_the_limit():
    """A loose (but enforced) limit: the same move is legal and is kept."""
    router, p, _n = _nudge_board(limit=1.0)

    result = drc_verify_and_nudge(router)

    assert result.pair_skew_reverts == []
    assert p.vias[0].y != pytest.approx(Y)
    assert LIMIT < _skew(router) <= 1.0


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (0.05, 0.09, None),  # moved, still inside the limit
        (0.05, 0.15, "newly_exceeds"),
        (0.20, 0.205, None),  # already failing, within the 0.01 mm budget
        (0.20, 0.25, "worsened"),
        (0.20, 0.10, None),  # improved
        (None, 0.50, None),  # was unrouted: nothing to compare
    ],
)
def test_classify_skew_change(before, after, expected):
    assert ppi.classify_skew_change(before, after, LIMIT) == expected


def test_advisory_skew_limit_is_not_guarded():
    """Only pairs with a skew target are guarded (Issue #7893).

    ETHERNET has no operating point in the phase rule, so the pair has no
    derived budget, and its protocol skew default is uncited: ``kct check``
    only warns, and the guard leaves such a pair alone.
    """
    router, p, _n = _nudge_board()
    [guarded] = ppi.guarded_pairs(router)
    assert guarded.target.cap_mm == pytest.approx(LIMIT)
    assert guarded.target.budget_ps == pytest.approx(2.0)  # DQS: UG583 timing limit
    advisory = {
        name: NetClassRouting(name="ETH", pair_protocol="ETHERNET", diffpair_partner=partner)
        for name, partner in (("DQS_P", "DQS_N"), ("DQS_N", "DQS_P"))
    }
    router.net_class_map = advisory
    assert router._pair_skew_target("DQS_P", "DQS_N").enforced is False
    assert ppi.guarded_pairs(router) == []
    result = drc_verify_and_nudge(router)
    assert result.pair_skew_reverts == []
    assert p.vias[0].y != pytest.approx(Y)


def test_retune_rejects_a_pass_that_breaks_an_unselected_pair():
    before = {("DQS_P", "DQS_N"): 0.05}
    with pytest.raises(RetuneError) as exc:
        _require_no_pair_skew_regression(before, {("DQS_P", "DQS_N"): 0.30}, _class_map())
    assert exc.value.code == "pair_skew_regression"
    assert "DQS_P/DQS_N" in exc.value.message
    # Inside the limit, or already failing within the budget: accepted.
    _require_no_pair_skew_regression(before, {("DQS_P", "DQS_N"): 0.08}, _class_map())
    _require_no_pair_skew_regression(
        {("DQS_P", "DQS_N"): 0.30}, {("DQS_P", "DQS_N"): 0.305}, _class_map()
    )
