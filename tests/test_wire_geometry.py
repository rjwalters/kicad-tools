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


class TestJunctionGatedUnion:
    """Junction-dot gating of wire-to-wire union (issue #4226).

    KiCad merges the nets of two touching/overlapping wires only where a
    junction dot is present; #4157's pure-geometry union over-merged
    dot-less grazes (board-05: 85/205 pins on +24V).  When
    ``junction_points`` is supplied, ``wire_segments_connect`` must union a
    T-touch / collinear overlap only if a junction dot sits at the
    touch/overlap point; the identical geometry WITHOUT a dot must NOT
    union.  Passing ``junction_points=None`` (the default) keeps the
    ungated pure-geometry predicate for the lint checker.
    """

    def test_t_touch_with_junction_unions(self):
        # B's endpoint (106,100) on A's interior WITH a dot at the touch.
        assert (
            wire_segments_connect(
                (100, 100),
                (112, 100),
                (106, 100),
                (106, 90),
                junction_points={(106, 100)},
            )
            is True
        )

    def test_t_touch_without_junction_does_not_union(self):
        # Same T-touch geometry, but no dot at (106,100) -> KiCad does NOT
        # merge (the board-05 dot-less graze that caused the false shorts).
        assert (
            wire_segments_connect(
                (100, 100),
                (112, 100),
                (106, 100),
                (106, 90),
                junction_points=set(),
            )
            is False
        )

    def test_t_touch_junction_at_wrong_point_does_not_union(self):
        # A dot exists, but at the far (non-touch) endpoint of B, not at the
        # touch coordinate -> must NOT union (issue #4226 edge case).
        assert (
            wire_segments_connect(
                (100, 100),
                (112, 100),
                (106, 100),
                (106, 90),
                junction_points={(106, 90)},
            )
            is False
        )

    def test_collinear_overlap_with_junction_in_range_unions(self):
        # A=[100,109], B=[103,112] share [103,109]; dot at (105,100) inside
        # the shared sub-segment -> unions.
        assert (
            wire_segments_connect(
                (100, 100),
                (109, 100),
                (103, 100),
                (112, 100),
                junction_points={(105, 100)},
            )
            is True
        )

    def test_collinear_overlap_without_junction_does_not_union(self):
        assert (
            wire_segments_connect(
                (100, 100),
                (109, 100),
                (103, 100),
                (112, 100),
                junction_points=set(),
            )
            is False
        )

    def test_collinear_overlap_junction_at_far_endpoint_does_not_union(self):
        # The exact board-05 wires-15/16 failure: two rail-drop stubs share
        # an X column and overlap by coincidence; each has a junction at its
        # OWN far end (outside the shared sub-segment), but none inside the
        # overlap -> must NOT union.  A=[100,109], B=[103,112] share
        # [103,109]; dots only at (100,100) (A's far end) and (112,100)
        # (B's far end).
        assert (
            wire_segments_connect(
                (100, 100),
                (109, 100),
                (103, 100),
                (112, 100),
                junction_points={(100, 100), (112, 100)},
            )
            is False
        )

    def test_shared_endpoint_never_needs_junction(self):
        # Endpoint-only touch is handled by endpoint Union-Find, so it
        # returns False here regardless of junction gating (no dot required
        # for legitimate endpoint connections).
        assert (
            wire_segments_connect(
                (100, 100),
                (106, 100),
                (106, 100),
                (112, 100),
                junction_points=set(),
            )
            is False
        )
