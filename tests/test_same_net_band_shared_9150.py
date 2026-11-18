"""Issue #9150: the shared same-net slit-band predicate boards 06/07 route through.

The recipes used to carry ~10 shape-identical inline copies of
``1e-6 < geom.distance(other) < trace_edge + 1.5e-3``; these tests pin the
shared form to that expression so a band-semantics change lands in one place.
"""

from __future__ import annotations

import itertools
import logging

import pytest

shapely_geometry = pytest.importorskip("shapely.geometry")
LineString = shapely_geometry.LineString
Point = shapely_geometry.Point

from kicad_tools.router import via_clearance  # noqa: E402
from kicad_tools.router.via_clearance import (  # noqa: E402
    SAME_NET_BAND_WRITE_SLACK_MM,
    same_net_band_edges,
    same_net_band_seat_ok,
    same_net_trace_band_clear,
)

EDGE = 0.1016


def _legacy_inline(geom, others, edge: float) -> bool:
    """The expression boards 06/07 inlined before #9150 (edge > 0 case)."""
    return not any(1e-6 < geom.distance(o) < edge + 1.5e-3 for o in others)


def test_slack_matches_the_historical_literal():
    assert SAME_NET_BAND_WRITE_SLACK_MM == 1.5e-3


def test_band_edges_resolve_from_the_jlcpcb_profile():
    pad_edge, trace_edge = same_net_band_edges("jlcpcb", 4, 1.0, 0.5)
    assert pad_edge > 0
    assert trace_edge > 0


def test_missing_stackup_entry_disables_the_band():
    """A stackup the fab profile has no entry for yields (0.0, 0.0), not a guess (#9188)."""
    assert same_net_band_edges("jlcpcb", 3, 1.0, 0.5) == (0.0, 0.0)


def test_missing_stackup_entry_logs_a_warning(caplog):
    """The disabled band is visible: the unresolved stackup is named in a warning (#9226)."""
    with caplog.at_level(logging.WARNING, logger=via_clearance.__name__):
        assert same_net_band_edges("jlcpcb", 3, 1.0, 0.5) == (0.0, 0.0)
    warned = [rec.getMessage() for rec in caplog.records]
    assert any(
        "profile='jlcpcb'" in msg
        and "layers=3" in msg
        and "outer_oz=1.0" in msg
        and "inner_oz=0.5" in msg
        for msg in warned
    )


def test_resolved_stackup_entry_logs_no_warning(caplog):
    """A stackup the profile resolves emits nothing (#9226)."""
    with caplog.at_level(logging.WARNING, logger=via_clearance.__name__):
        pad_edge, trace_edge = same_net_band_edges("jlcpcb", 4, 1.0, 0.5)
    assert pad_edge > 0
    assert trace_edge > 0
    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    ("gap", "expected_clear"),
    [
        (0.0, True),  # touching: one union
        (-0.1, True),  # overlap: one union
        (5e-7, True),  # float noise under the union epsilon
        (0.01, False),  # in band
        (EDGE, False),  # inside edge + write slack
        (EDGE + 1.4e-3, False),
        (EDGE + 1.6e-3, True),  # beyond the slacked edge
        (0.5, True),
    ],
)
def test_trace_band_boundaries(gap, expected_clear):
    annulus = Point(0, 0).buffer(0.25)
    other = Point(0.25 + gap + 0.3, 0).buffer(0.3)  # edge-to-edge gap == ``gap``
    assert same_net_trace_band_clear(annulus, [other], EDGE) is expected_clear


def test_trace_band_matches_legacy_inline_expression_on_a_grid():
    seg = LineString([(-3, 0.6), (3, 0.6)]).buffer(0.075)
    via = Point(1.5, 0).buffer(0.225)
    xs = [i * 0.0125 for i in range(-120, 121)]
    for x, y in itertools.product(xs, [0.0, 0.3, 0.45]):
        probe = Point(x, y).buffer(0.225)
        assert same_net_trace_band_clear(probe, [seg, via], EDGE) == _legacy_inline(
            probe, [seg, via], EDGE
        )


def test_edge_zero_disables_the_trace_band():
    annulus = Point(0, 0).buffer(0.25)
    in_band = Point(0.25 + 0.0005 + 0.3, 0).buffer(0.3)
    assert same_net_trace_band_clear(annulus, [in_band], 0.0)
    assert same_net_band_seat_ok(0, 0, 0.5, [in_band], [], 0.0, 0.0)


def test_seat_ok_accepts_a_generator_and_grades_pads_and_traces():
    pad = [(-1.0, -1.0, 1.0, 1.0)]
    seg = LineString([(0, 1.65), (3, 1.65)]).buffer(0.075)
    assert not same_net_band_seat_ok(1.3, 0, 0.5, iter([]), pad, 0.15, 0.15)
    assert same_net_band_seat_ok(1.5, 0, 0.5, iter([]), pad, 0.15, 0.15)
    assert not same_net_band_seat_ok(1.3, 2.0, 0.5, (g for g in [seg]), [], 0.15, 0.15)
    assert via_clearance.same_net_band_seat_ok is same_net_band_seat_ok
