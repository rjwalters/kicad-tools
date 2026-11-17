"""Fracture bridges are not fill edges in the same-net gate (Issue #6409).

KiCad writes a holed ``filled_polygon`` as one fractured ring: each hole is
joined to the outer outline by a pair of coincident, opposite-direction
edges.  Even-odd parity ignores the pair, but
``_point_on_same_net_fill``'s margin test used to treat it as a real pour
edge, rejecting legal weld sites a bridge happened to pass near (board 03:
VBUS U1.7 at 0.079 mm, VCC R14.1 at 0.085 mm).
"""

from __future__ import annotations

import pytest

from kicad_tools.cli import stitch_cmd
from kicad_tools.cli.stitch_cmd import (
    FilledPolygon,
    _bridge_edge_indices,
    _point_has_edge_margin,
    _point_on_same_net_fill,
    _polygon_index,
    _PolygonIndex,
)

RADIUS = 0.3
LAYER = "In2.Cu"


def _densify(ring: list[tuple[float, float]], steps: int) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for i, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(i + 1) % len(ring)]
        for s in range(steps):
            t = s / steps
            out.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    return out


def _fractured_fill(steps: int) -> list[tuple[float, float]]:
    """A 20x10 mm pour with a 4x2 mm hole, fractured the way KiCad does it.

    The bridge runs horizontally at y = 5 from the outline's left edge
    (0, 5) to the hole's left edge (8, 5), and back.  ``steps`` densifies
    the outline so the ring can be pushed above or below the index
    threshold.
    """
    # Outer outline, CCW, starting and ending at the bridge foot (0, 5).
    outer = [(0.0, 5.0), (0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)]
    pts = _densify(outer, steps)
    # Back at the bridge foot (0, 5): bridge in to the hole, walk the hole,
    # and let the ring's implicit wrap (8, 5) -> pts[0] == (0, 5) be the
    # exact reverse twin of the inbound bridge.
    pts.append((0.0, 5.0))
    pts.extend([(8.0, 5.0), (8.0, 6.0), (12.0, 6.0), (12.0, 4.0), (8.0, 4.0), (8.0, 5.0)])
    return pts


@pytest.fixture(params=["indexed", "linear"])
def ring(request) -> list[tuple[float, float]]:
    pts = _fractured_fill(steps=20 if request.param == "indexed" else 1)
    if request.param == "indexed":
        assert isinstance(_polygon_index(pts), _PolygonIndex)
    else:
        assert _polygon_index(pts) is None
    return pts


def _fill(points) -> list[FilledPolygon]:
    return [FilledPolygon(net_number=7, net_name="VBUS", layer=LAYER, points=list(points))]


def test_fixture_has_exactly_one_bridge_pair(ring):
    bridges = _bridge_edge_indices(ring)
    assert len(bridges) == 2
    n = len(ring)
    segs = {(ring[i], ring[(i + 1) % n]) for i in bridges}
    assert segs == {((0.0, 5.0), (8.0, 5.0)), ((8.0, 5.0), (0.0, 5.0))}


def test_via_near_bridge_is_on_fill(ring):
    """0.08 mm from the bridge, >= radius from every real edge: accepted.

    Before #6409 this returned False -- the bridge counted as a pour edge.
    """
    assert _point_on_same_net_fill(4.0, 5.08, RADIUS, _fill(ring), LAYER)
    assert _point_on_same_net_fill(4.0, 4.92, RADIUS, _fill(ring), LAYER)
    assert _point_has_edge_margin(4.0, 5.08, ring, RADIUS)


def test_via_overlapping_hole_is_rejected(ring):
    # Centre in copper 0.1 mm left of the hole: the pad overhangs the void.
    assert not _point_on_same_net_fill(7.9, 5.3, RADIUS, _fill(ring), LAYER)
    # Centre inside the hole.
    assert not _point_on_same_net_fill(10.0, 5.0, RADIUS, _fill(ring), LAYER)


def test_via_overlapping_real_outline_is_rejected(ring):
    assert not _point_on_same_net_fill(0.2, 2.0, RADIUS, _fill(ring), LAYER)
    assert not _point_on_same_net_fill(15.0, 9.85, RADIUS, _fill(ring), LAYER)
    # Near the bridge foot on the outline: the outline edge still enforces.
    assert not _point_on_same_net_fill(0.15, 5.08, RADIUS, _fill(ring), LAYER)


def test_well_clear_site_still_accepted(ring):
    assert _point_on_same_net_fill(15.0, 2.0, RADIUS, _fill(ring), LAYER)


def test_zone_outline_without_twins_has_no_bridges():
    square = _densify([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)], 20)
    assert _bridge_edge_indices(square) == frozenset()
    # Margin semantics unchanged for an untwinned ring.
    assert not _point_has_edge_margin(5.0, 0.1, square, RADIUS)
    assert _point_has_edge_margin(5.0, 5.0, square, RADIUS)


def test_index_and_linear_margin_agree_with_bridges(monkeypatch):
    pts = _fractured_fill(steps=20)
    samples = [(x / 4.0, y / 8.0) for x in range(-4, 85) for y in range(-4, 85)]
    indexed = [_point_has_edge_margin(x, y, pts, RADIUS) for x, y in samples]
    monkeypatch.setattr(stitch_cmd, "_POLYGON_INDEX_MIN_POINTS", 10**9)
    linear = [_point_has_edge_margin(x, y, pts, RADIUS) for x, y in samples]
    assert indexed == linear


def test_raw_index_query_still_sees_bridges_by_default():
    """Only the margin query opts out; clearance callers are unchanged."""
    pts = _fractured_fill(steps=20)
    index = _PolygonIndex(pts)
    assert index.has_edge_within_point(4.0, 5.08, RADIUS)
    assert not index.has_edge_within_point(4.0, 5.08, RADIUS, ignore_bridges=True)
