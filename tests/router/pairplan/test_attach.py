"""``pairplan.attach`` yields what the pre-refactor inline loops computed (#6837, 2c-iii).

Every caller takes the *first* legal candidate, so the yield order and values
are the contract.  The reference implementations below are the loops that used
to sit inline in ``relocate_pair_anchor``, ``sandwiched_pad_escape_sites`` and
``LatticePathfinder._pair_attach``.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.pairplan.attach import (
    anchor_shift_candidates,
    dogleg_attach_candidates,
    sandwich_via_site,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.quantize import dogleg_points


def _pad(x: float, y: float) -> Pad:
    return Pad(x=x, y=y, width=0.5, height=0.5, net=1, net_name="N")


def _reference_shifts(pad_p, pad_n, toward, half_pitch, step, max_shift):
    mid = ((pad_p.x + pad_n.x) / 2.0, (pad_p.y + pad_n.y) / 2.0)
    ux, uy = pad_p.x - pad_n.x, pad_p.y - pad_n.y
    axis_len = (ux * ux + uy * uy) ** 0.5
    if axis_len <= 1e-9:
        return []
    ux, uy = ux / axis_len, uy / axis_len
    nx, ny = -uy, ux
    if (toward[0] - mid[0]) * nx + (toward[1] - mid[1]) * ny < 0:
        nx, ny = -nx, -ny
    out = []
    for k in range(1, int(max_shift / step + 0.5) + 1):
        shift = k * step
        anchor = (mid[0] + nx * shift, mid[1] + ny * shift)
        out.append(
            (
                anchor,
                (anchor[0] + ux * half_pitch, anchor[1] + uy * half_pitch),
                (anchor[0] - ux * half_pitch, anchor[1] - uy * half_pitch),
            )
        )
    return out


@pytest.mark.parametrize("target", [(3.0, 1.0), (0.5, 4.0), (-2.0, -2.0), (5.0, 0.0)])
def test_dogleg_order_is_diagonal_first_then_axis_first(target) -> None:
    got = list(dogleg_attach_candidates((0.3, 0.7), target))
    assert got == [
        dogleg_points(0.3, 0.7, target[0], target[1], axis_first=False),
        dogleg_points(0.3, 0.7, target[0], target[1], axis_first=True),
    ]


def test_dogleg_candidates_differ_for_an_unaligned_target() -> None:
    first, second = dogleg_attach_candidates((0.0, 0.0), (3.0, 1.0))
    assert first != second


@pytest.mark.parametrize(
    "p,n,toward",
    [
        ((1.0, 0.0), (-1.0, 0.0), (0.0, 5.0)),  # horizontal axis, toward +y
        ((1.0, 0.0), (-1.0, 0.0), (0.0, -5.0)),  # flip branch
        ((0.0, 0.4), (0.0, -0.4), (3.0, 0.0)),  # vertical axis
        ((1.0, 1.0), (-0.5, -0.2), (2.0, -3.0)),  # skewed axis
    ],
)
def test_anchor_shifts_match_inline_loop(p, n, toward) -> None:
    pad_p, pad_n = _pad(*p), _pad(*n)
    got = list(
        anchor_shift_candidates(pad_p, pad_n, toward, half_pitch=0.3, step=0.05, max_shift=4.0)
    )
    assert got == _reference_shifts(pad_p, pad_n, toward, 0.3, 0.05, 4.0)
    assert len(got) == 80  # int(4.0 / 0.05 + 0.5)


def test_anchor_shift_sequence_is_k_times_step() -> None:
    pad_p, pad_n = _pad(1.0, 0.0), _pad(-1.0, 0.0)
    anchors = [
        a
        for a, _, _ in anchor_shift_candidates(
            pad_p, pad_n, (0.0, 9.0), half_pitch=0.2, step=0.25, max_shift=1.0
        )
    ]
    assert anchors == [(0.0, 0.25), (0.0, 0.5), (0.0, 0.75), (0.0, 1.0)]


def test_anchor_shift_rounding_includes_the_half_step() -> None:
    pad_p, pad_n = _pad(1.0, 0.0), _pad(-1.0, 0.0)
    # 0.37 / 0.1 + 0.5 = 4.2 -> 4 candidates; 0.38 / 0.1 + 0.5 = 4.3 -> 4 as well,
    # 0.45 / 0.1 + 0.5 = 5.0 -> 5.
    for max_shift, n in ((0.37, 4), (0.45, 5)):
        got = list(
            anchor_shift_candidates(
                pad_p, pad_n, (0.0, 1.0), half_pitch=0.1, step=0.1, max_shift=max_shift
            )
        )
        assert len(got) == n


def test_anchor_shift_flips_toward_the_far_end() -> None:
    pad_p, pad_n = _pad(1.0, 0.0), _pad(-1.0, 0.0)
    up = next(
        iter(anchor_shift_candidates(pad_p, pad_n, (0, 5), half_pitch=0.1, step=0.1, max_shift=1))
    )
    down = next(
        iter(anchor_shift_candidates(pad_p, pad_n, (0, -5), half_pitch=0.1, step=0.1, max_shift=1))
    )
    assert up[0][1] > 0 > down[0][1]


def test_anchor_shift_toward_on_the_axis_does_not_flip() -> None:
    # ``toward`` exactly on the P-N axis: the dot product with the normal is 0,
    # which is not ``< 0``, so the default (+normal) side is kept.
    pad_p, pad_n = _pad(1.0, 0.0), _pad(-1.0, 0.0)
    got = list(
        anchor_shift_candidates(pad_p, pad_n, (7.0, 0.0), half_pitch=0.1, step=0.5, max_shift=1.0)
    )
    assert got == _reference_shifts(pad_p, pad_n, (7.0, 0.0), 0.1, 0.5, 1.0)
    assert got[0][0] == (0.0, 0.5)


def test_leg_ends_straddle_the_anchor_along_the_axis() -> None:
    pad_p, pad_n = _pad(1.0, 0.0), _pad(-1.0, 0.0)
    anchor, end_p, end_n = next(
        iter(anchor_shift_candidates(pad_p, pad_n, (0, 5), half_pitch=0.3, step=0.1, max_shift=1))
    )
    assert end_p == pytest.approx((anchor[0] + 0.3, anchor[1]))
    assert end_n == pytest.approx((anchor[0] - 0.3, anchor[1]))


def test_degenerate_axis_yields_nothing() -> None:
    pad = _pad(1.0, 1.0)
    assert (
        list(
            anchor_shift_candidates(
                pad, _pad(1.0, 1.0), (0, 5), half_pitch=0.1, step=0.1, max_shift=1
            )
        )
        == []
    )


def test_max_shift_below_step_yields_nothing() -> None:
    pad_p, pad_n = _pad(1.0, 0.0), _pad(-1.0, 0.0)
    got = anchor_shift_candidates(pad_p, pad_n, (0, 5), half_pitch=0.1, step=0.5, max_shift=0.2)
    assert list(got) == []


@pytest.mark.parametrize("normal", [(0.0, 1.0), (0.0, -1.0), (math.sqrt(0.5), math.sqrt(0.5))])
def test_sandwich_via_site_matches_inline_expression(normal) -> None:
    pad = _pad(2.0, 3.0)
    hw, hh = 0.4, 0.2
    reach = abs(normal[0]) * hw + abs(normal[1]) * hh
    got = sandwich_via_site(pad, normal, 0.25, reach)
    assert got == (pad.x + normal[0] * (reach + 0.25), pad.y + normal[1] * (reach + 0.25))
