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

import itertools

import pytest

from kicad_tools.router import pair_postpass_invariant as ppi
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPair, DifferentialSignal
from kicad_tools.router.diffpair_routing import DiffPairRouter
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
    return skew


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
# Via-hairpin collapse (#7782/#7808 commit path)
# ---------------------------------------------------------------------------

STEP = 0.127
HY = 25.451


def _hairpin_p() -> Route:
    """Board 07's DQS_P near end: F.Cu runs past the turn to the via, B.Cu returns."""

    def seg(x1: float, y1: float, x2: float, y2: float, layer: Layer) -> Segment:
        return Segment(x1, y1, x2, y2, 0.1, layer, net=1, net_name="DQS_P")

    segs = [seg(24.981, HY, 25.108, HY, F)]
    xs = [25.108 + STEP * i for i in range(6)]
    segs += [seg(a, HY, b, HY, F) for a, b in itertools.pairwise(xs)]
    segs += [seg(b, HY, a, HY, B) for a, b in itertools.pairwise(xs)]
    ys = [HY - STEP * i for i in range(13)]
    segs += [seg(25.108, a, 25.108, b, B) for a, b in itertools.pairwise(ys)]
    segs.append(seg(25.108, ys[-1], 30.0, ys[-1], B))
    via = Via(25.743, HY, 0.3, 0.6, (F, B), net=1, net_name="DQS_P")
    return Route(net=1, net_name="DQS_P", segments=segs, vias=[via])


def _hairpin_board() -> tuple[Autorouter, DifferentialPair, Route, Route]:
    rules = DesignRules(trace_width=0.1, trace_clearance=0.1, grid_resolution=STEP)
    router = Autorouter(width=40.0, height=40.0, rules=rules)
    end_y = HY - 12 * STEP
    router.add_component(
        "U1", [_pad("1", 24.981, HY, 1, "DQS_P", 0.4), _pad("2", 5.0, 5.0, 2, "DQS_N", 0.4)]
    )
    router.add_component("U2", [_pad("1", 30.0, end_y, 1, "DQS_P", 0.4)])
    router.net_names.update({1: "DQS_P", 2: "DQS_N"})
    router.net_class_map = _class_map()
    p = _hairpin_p()
    measure = ppi.checker_length_measure(router)
    assert measure is not None
    # N is a plain straight leg exactly as long as P *with* its hairpin, so
    # collapsing P's hairpin alone is what skews the pair.
    length = measure(p)
    router.add_component("U3", [_pad("1", 5.0 + length, 5.0, 2, "DQS_N", 0.4)])
    n = Route(
        net=2,
        net_name="DQS_N",
        segments=[Segment(5.0, 5.0, 5.0 + length, 5.0, 0.1, F, net=2, net_name="DQS_N")],
    )
    for route in (p, n):
        router.grid.mark_route(route)
        router.routes.append(route)
    pair = DifferentialPair(
        name="DQS",
        positive=DifferentialSignal("DQS_P", 1, "DQS", "P", "pn_suffix"),
        negative=DifferentialSignal("DQS_N", 2, "DQS", "N", "pn_suffix"),
    )
    return router, pair, p, n


def test_hairpin_collapse_without_pair_still_edits():
    """The pre-#6447 call (no pair) keeps the #7782 behaviour: P loses its hairpin."""
    router, _pair, p, _n = _hairpin_board()
    assert _skew(router) <= 1e-6

    assert DiffPairRouter(router)._collapse_committed_via_hairpins([p]) >= 1
    assert _skew(router) > LIMIT


def test_hairpin_collapse_guard_undoes_an_edit_that_skews_the_pair():
    router, pair, p, n = _hairpin_board()
    before = p.copy_geometry()

    assert DiffPairRouter(router)._collapse_committed_via_hairpins([p, n], pair) == 0

    assert p.segments == before.segments and p.vias == before.vias
    assert _skew(router) <= 1e-6
    # The R-tree is keyed on the live objects again, not the discarded edit.
    grid = router.grid
    if grid._via_rtree is not None:
        assert id(p.vias[0]) in grid._via_rtree_items


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
    """Only limits ``kct check`` fails the board on are guarded.

    LVDS's protocol skew default is uncited, so ``kct check`` reports it as a
    warning and the guard leaves such a pair alone.
    """
    assert ppi.checker_skew_limit_mm(_class_map(), "DQS_P", "DQS_N") == pytest.approx(LIMIT)
    advisory = {
        name: NetClassRouting(name="LV", pair_protocol="LVDS", diffpair_partner=partner)
        for name, partner in (("DQS_P", "DQS_N"), ("DQS_N", "DQS_P"))
    }
    assert ppi.checker_skew_limit_mm(advisory, "DQS_P", "DQS_N") is None
    router, p, _n = _nudge_board()
    router.net_class_map = advisory
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
