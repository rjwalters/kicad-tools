"""Tangent (``gap == 0``) seats of the same-net pad slit band (Issue #9124).

Board 06's PCIE_RX+ via sat with its centre exactly one annulus radius from
its own pad's face: the disc touches the pad at a single corner point, the
predicate's ``gap`` was exactly 0, and it was ACCEPTED as a union -- while
the fab-view checker measured the wedge under the corner at 0.0011 mm.
Touch-at-a-point is not a copper union.

Pinned here, across the Python point predicate, the region (ring) form and
the C++ mirror:

* tangent seats (face-aligned locus, corner point) are REJECTED;
* real overlap (``gap < 0``) and clearance (``gap >= edge``) stay ACCEPTED;
* the segment branch is unchanged: ``gap == 0`` there means the centerline
  reaches the rectangle, a genuine overlap, and stays accepted;
* ``min_gap <= 0`` still turns the guard off.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.via_clearance import (
    same_net_pad_gap_acceptable,
    same_net_pad_gap_band_ring,
    same_net_segment_pad_gap_acceptable,
)

requires_cpp = pytest.mark.requires_router_cpp

RES = 0.05
COLS = ROWS = 400
CENTRE = 10.0
NET = 7
EDGE = 0.25
VIA_DIAMETER = 0.5  # annulus radius 0.25, dyadic
VIA_DRILL = 0.25

# 1.0 x 1.0 pad at the board centre: box (9.5, 9.5, 10.5, 10.5).
BBOX = (9.5, 9.5, 10.5, 10.5)
PAD = (CENTRE, CENTRE, 1.0, 1.0, NET, 0, 0.0, False)

# (x, y, accepted, why)
SEATS = [
    (10.75, 10.0, False, "face-aligned tangent, centre mid-face"),
    (10.75, 10.5, False, "face-aligned tangent at the pad's corner row"),
    (10.0, 10.75, False, "face-aligned tangent on the y face"),
    (9.25, 10.25, False, "face-aligned tangent, other face"),
    (10.5, 10.5, True, "centre on the corner: real overlap (gap -0.25)"),
    (10.6, 10.0, True, "annulus swallows the edge: real overlap"),
    (10.8, 10.0, False, "gap 0.05: ordinary slit"),
    (11.0, 10.0, True, "gap == edge: clear"),
    (11.5, 10.0, True, "well clear"),
]


def _cpp_pathfinder(edge_mm: float):
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(COLS, ROWS, 2, RES, 0.0, 0.0)
    x, y, w, h, net, layer_idx, rotation, circ = PAD
    grid.add_pad(x, y, w, h, net, layer_idx, 0, 0.0, False, rotation, circ)
    rules = router_cpp.DesignRules()
    rules.trace_width = 0.2
    rules.trace_clearance = 0.15
    rules.via_clearance = 0.15
    rules.via_drill = VIA_DRILL
    rules.via_diameter = VIA_DIAMETER
    rules.allow_smd_vias = True
    rules.same_net_pad_gap_band_edge_mm = edge_mm
    return router_cpp.Pathfinder(grid, rules, False)


@pytest.mark.parametrize(("x", "y", "accepted", "why"), SEATS)
def test_python_point_predicate(x, y, accepted, why):
    assert same_net_pad_gap_acceptable(x, y, VIA_DIAMETER, [BBOX], EDGE) is accepted, why


@pytest.mark.parametrize(("x", "y", "accepted", "why"), SEATS)
def test_ring_form_agrees_with_point_predicate(x, y, accepted, why):
    """A seat the ring covers is exactly a seat the predicate rejects."""
    from shapely.geometry import Point  # type: ignore[import-untyped]

    ring = same_net_pad_gap_band_ring(BBOX, VIA_DIAMETER, EDGE)
    # The ring is the region of the rejected seats, so ``accepted`` seats
    # lie outside it.  ``gap == edge`` sits on the ring's outer boundary;
    # ``intersects`` would count it, so test strict interior membership for
    # that one (the predicate's open band excludes it).
    covered = ring.contains(Point(x, y)) or (
        ring.boundary.distance(Point(x, y)) > 1e-12 and ring.intersects(Point(x, y))
    )
    if (x, y) == (11.0, 10.0):
        assert not covered, why
    else:
        assert covered is (not accepted), why


@requires_cpp
@pytest.mark.parametrize(("x", "y", "accepted", "why"), SEATS)
def test_cpp_mirror_matches_python(x, y, accepted, why):
    # Parity is exact on every seat above, but the two lower-edge epsilons
    # DELIBERATELY differ: Python (float64) rejects ``gap > -1e-9`` (real
    # overlap only), while the C++ mirror widens to ``kTangentEpsMm = 1e-4``
    # because its pad box edges arrive as float32 (ulp ~1.5e-5 mm at
    # 174 mm), so an exactly-tangent seat measures a few 1e-5 off 0 there.
    # A seat with a real overlap in ``(-1e-4, -1e-9]`` is therefore
    # Python-accepted and C++-rejected -- intended, erring conservative in
    # the mirror (types.hpp, Issue #9124).  Do not "fix" the 1e-4 into 1e-9.
    # Every SEATS row is dyadic so no row lands in that window, which is
    # also why the divergence cannot be pinned as a row here: the C++ seat
    # is rasterised to the grid (``RES``), which snaps any sub-1e-4 gap back
    # onto the tangent locus both sides reject.
    pf_on = _cpp_pathfinder(EDGE)
    pf_off = _cpp_pathfinder(0.0)
    gx, gy = round(x / RES), round(y / RES)
    assert not pf_off.is_via_blocked(gx, gy, NET, True), "guard-off baseline must be open"
    assert pf_on.is_via_blocked(gx, gy, NET, True) is (not accepted), why


def test_corner_point_tangent_is_rejected():
    """Centre on the diagonal exactly one radius from the corner point."""
    import math

    r = VIA_DIAMETER / 2
    x = BBOX[2] + r / math.sqrt(2)
    y = BBOX[3] + r / math.sqrt(2)
    assert same_net_pad_gap_acceptable(x, y, VIA_DIAMETER, [BBOX], EDGE) is False


def test_segment_branch_keeps_accepting_a_reaching_centerline():
    """Segment ``gap == 0`` is overlap (centerline reaches the rect): accepted."""
    # Trace from inside the pad out past its face: copper unions with the pad.
    assert same_net_segment_pad_gap_acceptable((10.0, 10.0), (11.5, 10.0), 0.1, [BBOX], EDGE)
    # Trace ending exactly at the face: still reaches the rectangle.
    assert same_net_segment_pad_gap_acceptable((10.5, 10.0), (11.5, 10.0), 0.1, [BBOX], EDGE)
    # A parallel skirt inside the band is still a slit.
    assert not same_net_segment_pad_gap_acceptable((10.6, 9.0), (10.6, 11.0), 0.05, [BBOX], EDGE)


def test_guard_off_accepts_tangent():
    assert same_net_pad_gap_acceptable(10.75, 10.0, VIA_DIAMETER, [BBOX], 0.0)
    assert same_net_pad_gap_acceptable(10.75, 10.0, VIA_DIAMETER, None, EDGE)
