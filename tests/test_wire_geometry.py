"""Unit tests for the wire-vs-wire union geometry primitive (issue #4143).

Directly exercise ``wire_segments_connect`` and its helpers so the geometry
math is covered independently of the connectivity-graph integration.
"""

from kicad_tools.schematic.models.wire_geometry import (
    _collinear_overlap,
    _point_on_segment_interior,
    wire_segments_connect,
)


class TestCollinearOverlap:
    def test_partial_overlap(self):
        # A=[100,109], B=[103,112] on y=100 share [103,109].
        assert _collinear_overlap((100, 100), (109, 100), (103, 100), (112, 100)) is True

    def test_full_containment_a_contains_b(self):
        assert _collinear_overlap((100, 100), (112, 100), (103, 100), (109, 100)) is True

    def test_full_containment_b_contains_a(self):
        assert _collinear_overlap((103, 100), (109, 100), (100, 100), (112, 100)) is True

    def test_shared_endpoint_only_is_not_overlap(self):
        # Touch at a single point (109,100) — zero-length overlap.
        assert _collinear_overlap((100, 100), (109, 100), (109, 100), (120, 100)) is False

    def test_disjoint_collinear_no_overlap(self):
        # Gap [105,110]: collinear but no shared sub-segment.
        assert _collinear_overlap((100, 100), (105, 100), (110, 100), (115, 100)) is False

    def test_parallel_but_offset_not_collinear(self):
        # Same X extent, different Y -> not on the same line.
        assert _collinear_overlap((100, 100), (112, 100), (100, 110), (112, 110)) is False

    def test_vertical_partial_overlap(self):
        assert _collinear_overlap((50, 100), (50, 109), (50, 103), (50, 112)) is True


class TestPointOnSegmentInterior:
    def test_interior_point(self):
        assert _point_on_segment_interior((106, 100), (100, 100), (112, 100)) is True

    def test_endpoint_excluded(self):
        assert _point_on_segment_interior((100, 100), (100, 100), (112, 100)) is False
        assert _point_on_segment_interior((112, 100), (100, 100), (112, 100)) is False

    def test_off_segment(self):
        assert _point_on_segment_interior((106, 105), (100, 100), (112, 100)) is False


class TestWireSegmentsConnect:
    def test_collinear_overlap_connects(self):
        assert wire_segments_connect((100, 100), (109, 100), (103, 100), (112, 100)) is True

    def test_t_touch_connects(self):
        # B's lower endpoint (106,100) on A's interior.
        assert wire_segments_connect((100, 100), (112, 100), (106, 100), (106, 90)) is True

    def test_shared_endpoint_not_reported(self):
        # Endpoint-only touch is handled by endpoint Union-Find, not here.
        assert wire_segments_connect((100, 100), (106, 100), (106, 100), (112, 100)) is False

    def test_bare_crossing_does_not_connect(self):
        # Two wires crossing mid-span (neither endpoint on the other) do NOT
        # connect in KiCad without a junction dot — only endpoint-on-interior
        # (T-touch) or collinear overlap unions.
        assert wire_segments_connect((100, 100), (112, 100), (106, 94), (106, 106)) is False

    def test_disjoint_no_connect(self):
        assert wire_segments_connect((100, 100), (105, 100), (110, 100), (115, 100)) is False
