"""Issue #7269: a coupled-committed pair's skew is bump-tuned when the comb fails.

On board 07 (``--differential-pairs``, grid engine) MIPI_DAT0 commits as a
pose-centreline trunk with 0.624 mm of skew against a 0.05 mm ``kct check``
limit.  The inline serpentine of ``route_differential_pair_coupled`` spans the
whole host segment at its 0.3 mm amplitude floor and cannot slide, so the
neighbouring copper beside the trunk rejects it, and the grid engine never runs
the post-route Phase 3I tuner.  The pair kept its skew.

The fix runs the audited bump tuner (``tune_diff_pair_skew``, the
``checker_skew_limit`` policy) on the committed pair when the comb is
rejected.  The fixture is MIPI_DAT0's shape: a short, via-free, tightly
coupled straight trunk with a small skew, plus foreign copper beside most of
the shorter half's host so only a sized, placed bump fits.
"""

from __future__ import annotations

import copy
import math

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPair, DifferentialSignal
from kicad_tools.router.diffpair_routing import (
    DiffPairRouter,
    _splice_tuned_segments,
    match_pair_lengths,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

W = 0.15  # board 07 MIPI trace width
GAP = 0.135  # board 07 MIPI intra-pair gap, edge to edge
PITCH = 0.381  # 3 cells at board 07's 0.127 mm grid
LIMIT = 0.05  # MIPI_CSI_LANES skew_tolerance_mm


def _seg(x1: float, y1: float, x2: float, y2: float, net: int, name: str) -> Segment:
    return Segment(x1, y1, x2, y2, W, Layer.F_CU, net=net, net_name=name)


def _length(route: Route) -> float:
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)


def _setup(skew: float = 0.62, *, skew_tolerance_mm: float | None = LIMIT, protocol=None):
    """A committed pair whose N half is ``skew`` mm shorter than P.

    P is committed (marked) first, as on board 07's MIPI_DAT0, so the cells
    under N's own trunk read as P's: every bulge the comb starts on N's trunk
    is "blocked by the partner".  Net 3 runs beside N on its outward side from
    x=8 to x=25, 0.45 mm above N's centreline, so a bump only fits on N's
    first ~2.4 mm and must pass the foreign-copper gate.
    """
    nc = NetClassRouting(
        name="MIPI",
        trace_width=W,
        clearance=0.10,
        intra_pair_clearance=GAP,
        coupled_routing=True,
        skew_tolerance_mm=skew_tolerance_mm,
        length_critical=True,
        pair_protocol=protocol,
    )
    ar = Autorouter(
        width=30.0,
        height=20.0,
        rules=DesignRules(trace_width=W, trace_clearance=0.127, grid_resolution=0.127),
    )
    ar.net_names = {1: "MIPI_P", 2: "MIPI_N", 3: "OTHER"}
    ar.net_class_map = {"MIPI_P": nc, "MIPI_N": nc}
    y = 10.0
    yn = y + PITCH
    p = Route(net=1, net_name="MIPI_P", segments=[_seg(5.0, y, 25.0, y, 1, "MIPI_P")])
    n = Route(net=2, net_name="MIPI_N", segments=[_seg(5.0 + skew, yn, 25.0, yn, 2, "MIPI_N")])
    other = Route(
        net=3, net_name="OTHER", segments=[_seg(8.0, yn + 0.45, 25.0, yn + 0.45, 3, "OTHER")]
    )
    for route in (p, n, other):
        ar._mark_route(route)
        ar.routes.append(route)
    pair = DifferentialPair(
        name="MIPI",
        positive=DifferentialSignal("MIPI_P", 1, "MIPI", "P", "pn_suffix"),
        negative=DifferentialSignal("MIPI_N", 2, "MIPI", "N", "pn_suffix"),
    )
    return ar, pair, p, n, other


def test_inline_comb_is_rejected_on_the_mipi_shaped_trunk():
    """The reproduction: the legacy comb cannot fix this pair."""
    ar, _pair, p, n, _other = _setup()
    p_copy, n_copy = copy.deepcopy(p), copy.deepcopy(n)
    assert not match_pair_lengths(
        p_copy, n_copy, 0.5, add_serpentines=True, intra_pair_clearance_mm=GAP, grid=ar.grid
    )


def test_committed_pair_is_bump_tuned_to_the_checker_limit():
    ar, pair, p, n, other = _setup()
    p_segments = p.segments
    assert abs(_length(p) - _length(n)) == pytest.approx(0.62)

    assert DiffPairRouter(ar)._tune_committed_pair_skew(pair)

    assert abs(_length(p) - _length(n)) <= LIMIT
    # Edited in place: the committed Route objects are the ones the router holds.
    assert ar.routes[0] is p and ar.routes[1] is n
    assert p.segments is p_segments  # the longer half is never touched
    # The bump sits clear of the foreign track (0.1 mm class clearance).
    from kicad_tools.core.geometry import segment_clearance

    foreign = other.segments[0]
    for s in n.segments:
        gap = segment_clearance(
            s.x1, s.y1, s.x2, s.y2, s.width, foreign.x1, foreign.y1, foreign.x2, foreign.y2, W
        )
        assert gap >= 0.1 - 1e-9
    # And it bulges away from the partner (P is below N).
    yn = 10.0 + PITCH
    assert all(min(s.y1, s.y2) >= yn - 1e-9 for s in n.segments)
    # The chain is still connected end to end.
    for a, b in zip(n.segments, n.segments[1:], strict=False):
        assert (a.x2, a.y2) == pytest.approx((b.x1, b.y1))
    # The new copper is on the grid as the pair's own net.
    top = [s for s in n.segments if min(s.y1, s.y2) > yn + 1e-6]
    assert top
    gx, gy = ar.grid.world_to_grid((top[0].x1 + top[0].x2) / 2, top[0].y1)
    assert ar.grid.cell_at(ar.grid.layer_to_index(Layer.F_CU.value), gy, gx).net == 2


def test_pair_within_the_limit_is_left_alone():
    ar, pair, _p, n, _other = _setup(skew=0.03)
    before = list(n.segments)
    assert not DiffPairRouter(ar)._tune_committed_pair_skew(pair)
    assert n.segments == before


def test_warning_severity_limit_is_not_tuned():
    # LVDS's protocol default is uncited, so ``kct check`` only warns.
    ar, pair, _p, n, _other = _setup(skew_tolerance_mm=None, protocol="LVDS")
    before = list(n.segments)
    assert not DiffPairRouter(ar)._tune_committed_pair_skew(pair)
    assert n.segments == before


def test_uncoupled_guard_still_rolls_back(monkeypatch):
    """A bump that breaks the #6795 uncoupled limit is refused as before."""
    import kicad_tools.validate.rules.diffpair_uncoupled_length as unc

    monkeypatch.setattr(unc, "uncoupled_limit", lambda thr, comps: (0.0, "test"))
    ar, pair, _p, n, _other = _setup()
    before = list(n.segments)
    assert not DiffPairRouter(ar)._tune_committed_pair_skew(pair)
    assert n.segments == before


def test_splice_maps_a_bump_back_onto_its_fragment():
    escape = Route(net=1, net_name="P", segments=[_seg(0, 0, 1, 0, 1, "P")], is_escape=True)
    main = Route(net=1, net_name="P", segments=[_seg(1, 0, 5, 0, 1, "P"), _seg(5, 0, 9, 0, 1, "P")])
    old = [*escape.segments, *main.segments]
    host = main.segments[0]
    bump = [_seg(1, 0, 2, 0, 1, "P"), _seg(2, 0, 2, -0.2, 1, "P"), _seg(2, -0.2, 5, 0, 1, "P")]
    new = [escape.segments[0], *bump, main.segments[1]]

    plan = _splice_tuned_segments([escape, main], old, new)

    assert plan is not None and len(plan) == 1
    fragment, segments = plan[0]
    assert fragment is main
    assert segments == [*bump, main.segments[1]]
    assert host not in segments


def test_splice_refuses_segments_it_cannot_place():
    main = Route(net=1, net_name="P", segments=[_seg(1, 0, 5, 0, 1, "P")])
    stray = Route(net=1, net_name="P", segments=[_seg(7, 0, 8, 0, 1, "P")])
    # ``old`` does not match the fragments: nothing is spliced.
    assert _splice_tuned_segments([main], [*main.segments, *stray.segments], main.segments) is None
