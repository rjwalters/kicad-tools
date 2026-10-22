"""Skew bumps on scarce host under the 2x excursion cap (issue #7521).

Under the cap (2x the pair gap, operator ruling Q35, issue #7649) each loop
adds little, so host length runs out before the skew closes.  Two fallbacks
keep every gate intact:

* partial bumps: when no bump that closes the whole skew fits, one that adds
  part of it is slid along the host (clear of a neighbour beside part of it)
  and the cascade spends its next insert on the rest (board 06's USB2_D+);
* the chamfer ladder: when the 0.55-width chamfered bumps cannot close the
  pair, the whole cascade reruns with tighter 45-degree chamfers, which add
  more length per mm of host (board 07's MIPI_DAT0 / MIPI_CLK).
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from kicad_tools.router import diffpair_length_tuning as tuning
from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairType,
    DifferentialSignal,
)
from kicad_tools.router.diffpair_bump_gates import MAX_EXCURSION_GAPS, MIN_JOG_WIDTHS
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length_tuning import (
    SKEW_BUMP_CHAMFER_LADDER,
    SKEW_BUMP_CHAMFER_WIDTHS,
    tune_diff_pair_skew,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment

W = 0.2
GAP = 0.15
PITCH = W + GAP


def _seg(x1, y1, x2, y2, net=1, name="D+"):
    return Segment(x1, y1, x2, y2, W, Layer.F_CU, net=net, net_name=name)


def _pair() -> DetectedPair:
    def sig(name, net_id, pol):
        return DifferentialSignal(
            net_name=name, net_id=net_id, base_name="D", polarity=pol, notation="plus_minus"
        )

    return DetectedPair(
        pair=DifferentialPair(
            name="D",
            positive=sig("D+", 1, "P"),
            negative=sig("D-", 2, "N"),
            pair_type=DifferentialPairType.USB2,
        ),
        source=DetectionSource.EXPLICIT,
    )


def _routes(host_mm: float, skew_mm: float) -> dict[int, Route]:
    """D+ one straight host; D- runs below it, ``skew_mm`` longer."""
    p = Route(net=1, net_name="D+", segments=[_seg(0, 0, host_mm, 0)])
    n = Route(
        net=2,
        net_name="D-",
        segments=[_seg(0, -PITCH, host_mm + skew_mm, -PITCH, 2, "D-")],
    )
    return {1: p, 2: n}


def _bump_copper(route: Route) -> list[Segment]:
    return [s for s in route.segments if abs(s.y1) > 1e-9 or abs(s.y2) > 1e-9]


def _assert_gated(route: Route) -> None:
    """Every committed bump obeys the excursion and jog gates."""
    copper = _bump_copper(route)
    assert copper
    assert max(abs(v) for s in copper for v in (s.y1, s.y2)) <= MAX_EXCURSION_GAPS * GAP + 1e-6
    for s in route.segments:
        flat = math.isclose(s.y1, s.y2, abs_tol=1e-9)
        if flat and abs(s.y1) > 1e-9:
            assert math.hypot(s.x2 - s.x1, 0) >= MIN_JOG_WIDTHS * W - 1e-6


def _chamfer_widths(route: Route) -> float:
    """The corner chamfer of the committed bumps, in trace widths."""
    diagonals = [
        math.hypot(s.x2 - s.x1, s.y2 - s.y1)
        for s in _bump_copper(route)
        if not math.isclose(s.x1, s.x2, abs_tol=1e-9) and not math.isclose(s.y1, s.y2, abs_tol=1e-9)
    ]
    assert diagonals
    return min(diagonals) / math.sqrt(2.0) / W


# ---------------------------------------------------------------------------
# Partial bumps beside a foreign via
# ---------------------------------------------------------------------------

#: Foreign vias on the bump side: one beside the host's start (board 06's
#: +3V3 pad) and one beside its middle (board 06's USB3_RX1+ via).
VIAS_X: list[float] = [1.0, 4.0]
VIA_Y, VIA_R, CLEARANCE = 0.40, 0.15, 0.1


def _via_clear(new_segments, _net, host_segments=None) -> bool:
    """No bump copper within ``CLEARANCE`` of a foreign via."""
    from kicad_tools.core.geometry import point_to_segment_distance

    return all(
        point_to_segment_distance(vx, VIA_Y, s.x1, s.y1, s.x2, s.y2) - s.width / 2 - VIA_R
        >= CLEARANCE - 1e-9
        for s in new_segments
        for vx in VIAS_X
    )


def _tune_beside_vias(host_mm: float = 8.0, skew_mm: float = 0.8):
    return tune_diff_pair_skew(
        _pair(),
        _routes(host_mm, skew_mm),
        tolerance_mm=0.1,
        intra_pair_clearance_mm=GAP,
        extra_clearance_ok=_via_clear,
    )


WIDE_ONLY = (SKEW_BUMP_CHAMFER_WIDTHS,)


def test_partial_bumps_slide_past_the_foreign_vias():
    """No full-need bump clears both vias; partial ones between them close it."""
    with patch.object(tuning, "SKEW_BUMP_CHAMFER_LADDER", WIDE_ONLY):
        p_out, _n, result = _tune_beside_vias()
    assert result.success, result.message
    assert result.skew_after_mm <= 0.1
    assert _via_clear(_bump_copper(p_out), 1)
    _assert_gated(p_out)


def test_without_partial_bumps_the_vias_block_the_pair():
    """The control: full-need bumps alone cannot clear the vias (the old cascade)."""
    with (
        patch.object(tuning, "SKEW_BUMP_CHAMFER_LADDER", WIDE_ONLY),
        patch.object(tuning, "_partial_placement_options", lambda **_kw: iter(())),
    ):
        _p, _n, result = _tune_beside_vias()
    assert not result.success
    assert result.reason == "post_insertion_drc_violation"


# ---------------------------------------------------------------------------
# Chamfer ladder
# ---------------------------------------------------------------------------


def test_ladder_starts_at_the_wide_chamfer_and_only_tightens():
    assert SKEW_BUMP_CHAMFER_LADDER[0] == SKEW_BUMP_CHAMFER_WIDTHS
    assert list(SKEW_BUMP_CHAMFER_LADDER) == sorted(SKEW_BUMP_CHAMFER_LADDER, reverse=True)
    assert all(c > 0.0 for c in SKEW_BUMP_CHAMFER_LADDER)  # always a 45-degree chamfer


def test_pair_the_wide_chamfer_closes_keeps_it():
    """A pair the 0.55-width bumps close is tuned exactly as before."""
    p_out, _n, result = tune_diff_pair_skew(
        _pair(), _routes(30.0, 1.0), tolerance_mm=0.1, intra_pair_clearance_mm=GAP
    )
    assert result.success, result.message
    assert _chamfer_widths(p_out) == pytest.approx(SKEW_BUMP_CHAMFER_WIDTHS, abs=1e-6)
    _assert_gated(p_out)


def test_ladder_closes_a_pair_the_wide_chamfer_cannot():
    """Too little host for 0.55-width loops; a tighter chamfer packs enough."""
    routes = _routes(12.0, 2.5)
    with patch.object(tuning, "SKEW_BUMP_CHAMFER_LADDER", WIDE_ONLY):
        _p, _n, wide = tune_diff_pair_skew(
            _pair(), routes, tolerance_mm=0.1, intra_pair_clearance_mm=GAP
        )
    assert not wide.success
    p_out, _n, result = tune_diff_pair_skew(
        _pair(), routes, tolerance_mm=0.1, intra_pair_clearance_mm=GAP
    )
    assert result.success, result.message
    assert result.skew_after_mm <= 0.1
    assert _chamfer_widths(p_out) < SKEW_BUMP_CHAMFER_WIDTHS
    _assert_gated(p_out)


def test_no_rung_closes_the_pair_reports_the_wide_attempt():
    """When even the tightest chamfer falls short, the 0.55-width result stands."""
    routes = _routes(6.0, 5.0)
    with patch.object(tuning, "SKEW_BUMP_CHAMFER_LADDER", WIDE_ONLY):
        _p, _n, wide = tune_diff_pair_skew(
            _pair(), routes, tolerance_mm=0.1, intra_pair_clearance_mm=GAP
        )
    _p, _n, result = tune_diff_pair_skew(
        _pair(), routes, tolerance_mm=0.1, intra_pair_clearance_mm=GAP
    )
    assert not result.success
    assert result.skew_after_mm == pytest.approx(wide.skew_after_mm)
    assert result.reason == wide.reason
