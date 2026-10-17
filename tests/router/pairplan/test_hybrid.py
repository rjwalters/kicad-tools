"""Trunk-start proposals for one-ended diff-pair hybrids (issue #6446).

Pure geometry, no engine: the ladder, its ordering, the leg bound, the cap and
determinism.  The engine side is ``tests/router/lattice/test_pair_hybrid_6446.py``.
"""

from __future__ import annotations

import math

from kicad_tools.router.pairplan import HEADINGS, TrunkStart, propose_trunk_starts
from kicad_tools.router.pairplan.hybrid import (
    HYBRID_MAX_LEG_MM,
    MAX_TRUNK_STARTS,
    TRUNK_SETBACKS_MM,
)

# A part whose pad field spans x 20..28, y 2..10; the pair sits at its centre
# and the other end is far to the west.
FIELD = (20.0, 2.0, 28.0, 10.0)
PAD_P, PAD_N = (24.0, 5.7), (24.0, 6.3)
TOWARD = (3.0, 6.0)
GAP = 0.42


def _starts(**kw: object) -> list[TrunkStart]:
    args: dict = {"site_gap": GAP, "max_leg_mm": HYBRID_MAX_LEG_MM}
    args.update(kw)
    return propose_trunk_starts(PAD_P, PAD_N, TOWARD, FIELD, **args)


def _centre(t: TrunkStart) -> tuple[float, float]:
    return ((t.site_p[0] + t.site_n[0]) / 2.0, (t.site_p[1] + t.site_n[1]) / 2.0)


def test_starts_leave_the_pad_field_toward_the_other_end() -> None:
    starts = _starts()
    assert starts
    for t in starts:
        cx, cy = _centre(t)
        # Outside the pad-field box, on the side facing the other end.
        assert not (FIELD[0] <= cx <= FIELD[2] and FIELD[1] <= cy <= FIELD[3])
        assert cx < FIELD[0]
        h = HEADINGS[t.heading]
        assert h[0] < 0.0  # heading west-ish


def test_ladder_is_nearest_setback_first_and_both_orderings_are_offered() -> None:
    starts = _starts()
    setbacks = [t.setback for t in starts]
    assert setbacks == sorted(setbacks)
    assert setbacks[0] == TRUNK_SETBACKS_MM[0]
    # Every position comes uncrossed then crossed, swapping P and N.
    for first, second in zip(starts[0::2], starts[1::2], strict=True):
        assert not first.crossed and second.crossed
        assert first.site_p == second.site_n and first.site_n == second.site_p


def test_sites_are_one_gap_apart_and_uncrossed_keeps_the_pad_axis_side() -> None:
    for t in _starts():
        assert abs(math.dist(t.site_p, t.site_n) - GAP) < 1e-9
    # The pad pair is vertical with P above N (smaller y): the uncrossed
    # ordering keeps P on that side of a westward trunk's axis.
    first = _starts()[0]
    assert HEADINGS[first.heading] == (-1.0, 0.0)
    assert first.site_p[1] < first.site_n[1]


def test_cap_bounds_the_number_of_positions() -> None:
    starts = _starts()
    assert len(starts) <= 2 * MAX_TRUNK_STARTS
    assert len(_starts(cap=2)) <= 4


def test_leg_bound_drops_far_positions() -> None:
    mid = (24.0, 6.0)
    for t in _starts(max_leg_mm=3.0):
        assert math.dist(_centre(t), mid) <= 3.0 + 1e-9
    # Nothing fits when even the nearest rung is beyond the bound.
    assert _starts(max_leg_mm=0.5) == []


def test_proposals_are_deterministic() -> None:
    assert _starts() == _starts()


def test_degenerate_target_falls_back_to_every_heading() -> None:
    # With the other end on top of the pair midpoint no heading is preferred.
    starts = propose_trunk_starts(
        PAD_P, PAD_N, (24.0, 6.0), FIELD, site_gap=GAP, max_leg_mm=HYBRID_MAX_LEG_MM
    )
    assert starts
