"""Pair centrelines must not reverse onto an end stub (issue #6975).

``_route_pair_impl`` partitions the end stubs by parity: the goal stub's
polarity bit assumes the centreline's FINAL travel direction is the reverse
of the stub polyline (and the start stub's assumes the first travel is along
it).  A heading-less A* could instead overshoot an end anchor and come back
along the stub -- a 180-degree reversal.  #6964 trims that reversal before
offsetting, but the trimmed centreline then ends travelling the OTHER way,
so the parity flips at that end only and every trimmed centreline failed
``pair-polarity-twist`` (34/34 across boards 03, 06rf and 07rf).

The fix keeps the parity contract in the search itself: in a parity-filtered
pair search a goal stub is only reachable along an edge that does not
reverse onto it, and a start stub's node never leaves back along it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kicad_tools.router.lattice import coupled as coupled_mod
from kicad_tools.router.lattice.coupled import side_bit

from .test_coupled_pairs import _pad, _pair_board


def _anchored_pair(*, swap_far_end: bool, wall_behind_a: bool):
    """Board-03-like end B: the centreline ends at an anchor in FRONT of
    the pads (like a pass-under gateway anchor), on a lattice node."""
    extra = [_pad(1.8, 6.0, 9, "WALL", ref="WA", width=0.4, height=12.0)] if wall_behind_a else []
    pf, pc = _pair_board(extra, swap_far_end=swap_far_end)
    nodes = pf.build().nodes
    anchor = min(nodes.values(), key=lambda p: (p[0] - 25.0) ** 2 + (p[1] - 5.5) ** 2)
    return pf, replace(pc, anchor_b=anchor)


@pytest.fixture
def centrelines(monkeypatch):
    """Record every raw centreline the pair finish gate sees."""
    seen: list[tuple[list, list]] = []
    orig = coupled_mod.trim_end_reversals

    def spy(points, *args, **kwargs):
        out = orig(points, *args, **kwargs)
        seen.append((coupled_mod.merge_collinear_points(points), out))
        return out

    monkeypatch.setattr(coupled_mod, "trim_end_reversals", spy)
    return seen


def _assert_no_end_reversal(seen) -> None:
    assert seen, "the pair search never produced a centreline"
    for raw, trimmed in seen:
        assert trimmed == raw, f"centreline reverses onto an end stub: {raw}"


@pytest.mark.parametrize(
    ("swap_far_end", "wall_behind_a"),
    [(False, False), (True, True)],
    ids=["first-parity-overshoot", "single-parity-overshoot"],
)
def test_pair_search_never_overshoots_an_end_anchor(
    centrelines, swap_far_end, wall_behind_a
) -> None:
    pf, pc = _anchored_pair(swap_far_end=swap_far_end, wall_behind_a=wall_behind_a)
    pf.route_netset([], coupled=[pc])
    _assert_no_end_reversal(centrelines)


def test_single_parity_pair_does_not_decline_as_polarity_twist(centrelines) -> None:
    # End A has a wall right behind its pads, so only the parity whose start
    # stubs leave toward B exists; end B's P/N are swapped and its anchor
    # sits in front of the pads.  The A* used to overshoot the anchor and
    # return along the goal stub -> trimmed -> pair-polarity-twist.
    pf, pc = _anchored_pair(swap_far_end=True, wall_behind_a=True)
    routes, _stats = pf.route_netset([], coupled=[pc])
    assert pf.pair_outcomes[pc.key] != "pair-polarity-twist", pf.pair_outcomes
    if pf.pair_outcomes[pc.key] == "coupled":
        route_p = routes[(pc.key, "P")]
        ends_p = {(s.x1, s.y1) for s in route_p.segments} | {(s.x2, s.y2) for s in route_p.segments}
        assert (pc.pad_p_a.x, pc.pad_p_a.y) in ends_p
        assert (pc.pad_p_b.x, pc.pad_p_b.y) in ends_p


def test_finished_centrelines_keep_the_stub_parity(centrelines) -> None:
    # The parity contract itself: every centreline that reaches the finish
    # gate ends travelling so that its end-B side bit equals its end-A one.
    pf, pc = _anchored_pair(swap_far_end=False, wall_behind_a=False)
    pf.route_netset([], coupled=[pc])
    assert centrelines
    for _raw, center in centrelines:
        first = (center[1][0] - center[0][0], center[1][1] - center[0][1])
        last = (center[-1][0] - center[-2][0], center[-1][1] - center[-2][1])
        assert side_bit(first, pc.pad_p_a, pc.pad_n_a) == side_bit(last, pc.pad_p_b, pc.pad_n_b)


def test_trimmed_tail_loop_chord_is_still_reverified(monkeypatch) -> None:
    """Issue #7931: a centerline cut by the tail-loop trim is re-verified.

    Every centerline the finish gate sees is replaced by a straight run
    between its two end anchors with a sub-pitch jog-and-return past the far
    anchor -- the board-03 tail shape.  The real trim removes the loop, and
    the remaining chord runs straight through a foreign pad sitting on the
    pair's axis.  The finish gate must reject it like any other centerline:
    the trim changes the centerline, never the verification.
    """
    blocker = _pad(15.0, 5.5, 9, "OTHER", ref="X1", width=1.0, height=1.0)
    pf, pc = _pair_board([blocker])
    orig = coupled_mod.trim_end_reversals
    trimmed: list[list] = []

    def looped(points, *args, **kwargs):
        p0, pe = points[0], points[-1]
        if abs(p0[1] - pe[1]) > 1e-9:
            return orig(points, *args, **kwargs)
        sx = 1.0 if pe[0] > p0[0] else -1.0
        loop = [p0, pe, (pe[0] + 0.05 * sx, pe[1] + 0.05), (pe[0] + 0.1 * sx, pe[1]), pe]
        out = orig(loop, *args, **kwargs)
        trimmed.append(out)
        return out

    monkeypatch.setattr(coupled_mod, "trim_end_reversals", looped)
    routes, _stats = pf.route_netset([], coupled=[pc])

    # The trim did fire: the loop was cut to the bare chord through X1.
    assert trimmed, "the finish gate never saw a straight-axis centerline"
    assert all(len(c) == 2 for c in trimmed), trimmed
    # ...and that chord was not shipped: nothing emitted touches X1's copper.
    assert pf.pair_outcomes[pc.key] != "coupled", pf.pair_outcomes
    for route in routes.values():
        for seg in route.segments:
            for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
                assert not (14.4 <= x <= 15.6 and 4.9 <= y <= 6.1), (x, y)
    assert pf.pair_outcomes[pc.key].startswith("pair-leg"), pf.pair_outcomes

    # Control: with the real centerlines (around X1) the pair couples, so the
    # decline above is the re-verified chord, not the board.
    monkeypatch.setattr(coupled_mod, "trim_end_reversals", orig)
    pf2, pc2 = _pair_board([blocker])
    pf2.route_netset([], coupled=[pc2])
    assert pf2.pair_outcomes[pc2.key] == "coupled", pf2.pair_outcomes
