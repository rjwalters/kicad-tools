"""Tests for the trace-copper polygon helper (Issue #4176)."""

from __future__ import annotations

import pytest

from kicad_tools.geometry.copper import segment_copper_polygon

pytest.importorskip("shapely")


def test_segment_polygon_has_expected_area():
    # A 10mm-long, 0.25mm-wide trace: rectangle 10 x 0.25 = 2.5 plus two
    # semicircular end caps of radius 0.125 (area pi * 0.125**2 ~= 0.049).
    poly = segment_copper_polygon((0.0, 0.0), (10.0, 0.0), 0.25)
    assert poly is not None
    expected = 10.0 * 0.25 + 3.141592653589793 * 0.125**2
    assert poly.area == pytest.approx(expected, rel=1e-3)


def test_two_touching_segments_intersect():
    a = segment_copper_polygon((0.0, 0.0), (5.0, 0.0), 0.25)
    b = segment_copper_polygon((5.0, 0.0), (5.0, 5.0), 0.25)
    assert a.intersects(b)


def test_two_apart_segments_do_not_intersect():
    # Endpoints 0.009mm apart but width 0.001 (buffer 0.0005 each side): the
    # copper does not bridge the gap.
    a = segment_copper_polygon((0.0, 0.0), (5.0, 0.0), 0.001)
    b = segment_copper_polygon((5.0, 0.009), (5.0, 5.0), 0.001)
    assert not a.intersects(b)


def test_zero_width_returns_line_geometry():
    poly = segment_copper_polygon((0.0, 0.0), (10.0, 0.0), 0.0)
    assert poly is not None
    assert poly.area == 0.0
    # A zero-length + zero-width segment reduces to a point.
    pt = segment_copper_polygon((1.0, 1.0), (1.0, 1.0), 0.0)
    assert pt is not None
    assert pt.area == 0.0


def test_zero_length_positive_width_is_disk():
    disk = segment_copper_polygon((1.0, 1.0), (1.0, 1.0), 0.5)
    assert disk is not None
    # Round end cap disk of radius 0.25.
    assert disk.area == pytest.approx(3.141592653589793 * 0.25**2, rel=1e-2)
