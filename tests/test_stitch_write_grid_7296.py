"""Stitch vias are clearance-checked where the writer puts them (Issue #7296).

The board writer rounds via coordinates to 0.01 mm (``sexp.builders.fmt``).
A candidate validated unrounded can be written up to ~7 um closer to foreign
copper than the clearance it passed: board 06 shipped a GND stitch via at
``y=64.415`` (0.2025 mm from a USB3_RX1+ track) written as ``64.41``
(0.1975 mm), under the router-declared 0.2 mm Power class.
"""

from __future__ import annotations

import math

from kicad_tools.cli.stitch_cmd import PadInfo, TrackSegment, calculate_via_stub

VIA_SIZE = 0.45
CLEARANCE = 0.2
TRACK_HALF = 0.1


def _pad() -> PadInfo:
    # First candidate: pad.x + (0.32 + 0.5) = 100.816 -> written as 100.82.
    return PadInfo(
        reference="U2",
        pad_number="A4",
        net_number=1,
        net_name="GND",
        x=99.996,
        y=100.0,
        layer="F.Cu",
        width=0.54,
        height=0.64,
    )


def _track(x: float) -> TrackSegment:
    return TrackSegment(
        start_x=x,
        start_y=98.0,
        end_x=x,
        end_y=102.0,
        width=2 * TRACK_HALF,
        layer="F.Cu",
        net_number=2,
    )


def _gap(via_x: float, track_x: float) -> float:
    return abs(track_x - via_x) - VIA_SIZE / 2 - TRACK_HALF


def test_candidate_is_on_the_write_grid_and_clears_as_written():
    # 0.001 mm of margin unrounded, 0.003 mm short once rounded toward it.
    track_x = 100.816 + VIA_SIZE / 2 + TRACK_HALF + CLEARANCE + 0.001
    assert _gap(100.816, track_x) >= CLEARANCE, "precondition: unrounded passes"
    assert _gap(round(100.816, 2), track_x) < CLEARANCE, "precondition: as written fails"

    placed = calculate_via_stub(
        _pad(),
        0.5,
        VIA_SIZE,
        [],
        CLEARANCE,
        other_net_tracks=[_track(track_x)],
    )

    assert placed is not None
    via_x, via_y, _corner = placed
    assert (via_x, via_y) == (round(via_x, 2), round(via_y, 2))
    distance = math.hypot(max(0.0, abs(track_x - via_x)), 0.0)
    assert distance - VIA_SIZE / 2 - TRACK_HALF >= CLEARANCE - 1e-9


def test_unobstructed_candidate_is_unchanged_apart_from_rounding():
    placed = calculate_via_stub(_pad(), 0.5, VIA_SIZE, [], CLEARANCE)
    assert placed is not None
    assert placed[:2] == (100.82, 100.0)
