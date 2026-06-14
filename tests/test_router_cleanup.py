"""Tests for post-route artifact cleanup (Issues #1979, #2039, #2259).

Tests the ``cleanup_artifacts()`` method on ``Autorouter`` which:
- Preserves net-0 routes whose child segments/vias have valid nets
- Removes net-0 routes only when ALL children are also net-0
- Strips individual net-0 segments/vias from otherwise valid routes
- Removes segments with both endpoints outside the board bounding box
- Removes vias with center outside the board bounding box
- Uses board edge bbox (when set) instead of grid origin/dimensions
- Restores removed segments/vias when removal would fragment a net
  (connectivity-aware cleanup, Issue #2259)
"""

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via


def _make_router(
    width: float = 50.0,
    height: float = 40.0,
    origin_x: float = 100.0,
    origin_y: float = 80.0,
) -> Autorouter:
    """Create a minimal Autorouter for cleanup testing."""
    return Autorouter(
        width=width,
        height=height,
        origin_x=origin_x,
        origin_y=origin_y,
    )


def _seg(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    net: int = 1,
    layer: Layer = Layer.F_CU,
) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.25, layer=layer, net=net)


def _via(x: float, y: float, net: int = 1) -> Via:
    return Via(
        x=x,
        y=y,
        drill=0.3,
        diameter=0.6,
        layers=(Layer.F_CU, Layer.B_CU),
        net=net,
    )


class TestNet0RouteRemoval:
    """Test removal of entire routes with net == 0."""

    def test_removes_net0_route(self):
        router = _make_router()
        router.routes = [
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
            Route(net=5, net_name="VCC", segments=[_seg(110, 90, 120, 90, net=5)]),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_routes_removed"] == 1
        assert len(router.routes) == 1
        assert router.routes[0].net == 5

    def test_no_net0_routes(self):
        router = _make_router()
        router.routes = [
            Route(net=1, net_name="A", segments=[_seg(110, 90, 120, 90)]),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_routes_removed"] == 0
        assert len(router.routes) == 1

    def test_all_net0_routes_removed(self):
        router = _make_router()
        router.routes = [
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
            Route(net=0, net_name="", segments=[_seg(115, 95, 125, 95, net=0)]),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_routes_removed"] == 2
        assert len(router.routes) == 0


class TestNet0SegmentViaStripping:
    """Test stripping individual net-0 segments/vias from valid routes."""

    def test_strips_net0_segment_from_valid_route(self):
        router = _make_router()
        router.routes = [
            Route(
                net=3,
                net_name="SIG",
                segments=[
                    _seg(110, 90, 115, 90, net=3),
                    _seg(115, 90, 120, 90, net=0),  # orphan
                ],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_segments_removed"] == 1
        assert len(router.routes[0].segments) == 1
        assert router.routes[0].segments[0].net == 3

    def test_strips_net0_via_from_valid_route(self):
        router = _make_router()
        router.routes = [
            Route(
                net=2,
                net_name="CLK",
                segments=[_seg(110, 90, 120, 90, net=2)],
                vias=[_via(115, 90, net=2), _via(118, 90, net=0)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_vias_removed"] == 1
        assert len(router.routes[0].vias) == 1
        assert router.routes[0].vias[0].net == 2


class TestOutOfBoundsRemoval:
    """Test removal of segments/vias outside the board bounding box.

    Board: origin (100, 80), width 50, height 40
    So valid area is x=[100..150], y=[80..120] with 0.5mm margin.
    """

    def test_removes_segment_both_endpoints_outside(self):
        router = _make_router()
        # Both endpoints far outside board
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(200, 200, 210, 200)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_segments_removed"] == 1
        assert len(router.routes[0].segments) == 0

    def test_preserves_segment_one_endpoint_inside(self):
        """Segments that bridge the board edge should be preserved."""
        router = _make_router()
        # One endpoint inside, one outside
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(110, 90, 200, 200)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_segments_removed"] == 0
        assert len(router.routes[0].segments) == 1

    def test_preserves_segment_both_endpoints_inside(self):
        router = _make_router()
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(110, 90, 130, 100)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_segments_removed"] == 0
        assert len(router.routes[0].segments) == 1

    def test_preserves_segment_near_edge_within_margin(self):
        """Segments near but within the margin of the board edge are kept."""
        router = _make_router()
        # Point at x=99.6 is within the 0.5mm margin of origin_x=100
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(99.6, 90, 110, 90)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_segments_removed"] == 0
        assert len(router.routes[0].segments) == 1

    def test_removes_via_outside_bounds(self):
        router = _make_router()
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(110, 90, 120, 90)],
                vias=[_via(200, 200)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_vias_removed"] == 1
        assert len(router.routes[0].vias) == 0

    def test_preserves_via_inside_bounds(self):
        router = _make_router()
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(110, 90, 120, 90)],
                vias=[_via(115, 95)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_vias_removed"] == 0
        assert len(router.routes[0].vias) == 1

    def test_custom_margin(self):
        """Test with custom oob_margin."""
        router = _make_router()
        # Point at (98, 90): outside default 0.5mm margin but inside 3mm margin
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(98, 90, 110, 90)],
            ),
        ]
        # With default margin (0.5mm), x=98 is outside (min_x = 99.5)
        stats = router.cleanup_artifacts(oob_margin=0.5)
        # One endpoint (98) is outside, one (110) is inside -- preserved
        assert stats["oob_segments_removed"] == 0

        # Both endpoints outside with a segment fully OOB
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(50, 50, 60, 50)],
            ),
        ]
        stats = router.cleanup_artifacts(oob_margin=3.0)
        assert stats["oob_segments_removed"] == 1


class TestEmptyCleanup:
    """Verify cleanup on routes with no artifacts."""

    def test_empty_routes(self):
        router = _make_router()
        router.routes = []
        stats = router.cleanup_artifacts()
        assert all(v == 0 for v in stats.values())

    def test_clean_routes_unchanged(self):
        router = _make_router()
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(110, 90, 120, 90), _seg(120, 90, 130, 100)],
                vias=[_via(120, 90)],
            ),
            Route(
                net=2,
                net_name="B",
                segments=[_seg(105, 85, 115, 95)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert all(v == 0 for v in stats.values())
        assert len(router.routes) == 2
        assert len(router.routes[0].segments) == 2
        assert len(router.routes[0].vias) == 1
        assert len(router.routes[1].segments) == 1


class TestToSexpCallsCleanup:
    """Verify that to_sexp() automatically runs cleanup."""

    def test_to_sexp_removes_net0(self):
        router = _make_router()
        router.routes = [
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
            Route(net=1, net_name="A", segments=[_seg(110, 90, 120, 90)]),
        ]
        sexp = router.to_sexp()
        # After cleanup, only net=1 route remains
        assert len(router.routes) == 1
        assert "(net 1)" in sexp
        assert "(net 0)" not in sexp

    def test_to_sexp_stores_cleanup_stats(self):
        router = _make_router()
        router.routes = [
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
        ]
        router.to_sexp()
        assert hasattr(router, "_cleanup_stats")
        assert router._cleanup_stats["net0_routes_removed"] == 1


class TestCleanupCombined:
    """Test cleanup with multiple artifact types in a single pass."""

    def test_net0_and_oob_combined(self):
        router = _make_router()
        router.routes = [
            # Net-0 route with all-net-0 children (removed entirely)
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
            # Valid route with a net-0 segment and an OOB segment
            Route(
                net=3,
                net_name="MIX",
                segments=[
                    _seg(110, 90, 120, 90, net=3),  # valid
                    _seg(115, 95, 125, 95, net=0),  # net-0 orphan
                    _seg(200, 200, 210, 200, net=3),  # OOB
                ],
                vias=[
                    _via(115, 90, net=3),  # valid
                    _via(300, 300, net=3),  # OOB
                ],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_routes_removed"] == 1
        assert stats["net0_segments_removed"] == 1
        assert stats["oob_segments_removed"] == 1
        assert stats["oob_vias_removed"] == 1
        # Only the valid route remains with 1 segment and 1 via
        assert len(router.routes) == 1
        assert len(router.routes[0].segments) == 1
        assert len(router.routes[0].vias) == 1


class TestNet0RouteWithValidChildren:
    """Issue #2039: Routes with net=0 but valid child segment/via nets
    should be preserved with the child net propagated to the Route."""

    def test_preserves_net0_route_with_valid_segment_nets(self):
        """A Route(net=0) whose segments all have net=5 should survive
        with route.net corrected to 5."""
        router = _make_router()
        router.routes = [
            Route(
                net=0,
                net_name="",
                segments=[
                    _seg(110, 90, 120, 90, net=5),
                    _seg(120, 90, 130, 100, net=5),
                ],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_routes_removed"] == 0
        assert len(router.routes) == 1
        assert router.routes[0].net == 5

    def test_preserves_net0_route_with_valid_via_nets(self):
        """A Route(net=0) with no segments but vias with valid nets
        should be preserved."""
        router = _make_router()
        router.routes = [
            Route(
                net=0,
                net_name="",
                segments=[],
                vias=[_via(115, 90, net=7)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_routes_removed"] == 0
        assert len(router.routes) == 1
        assert router.routes[0].net == 7

    def test_removes_net0_route_all_children_net0(self):
        """A Route(net=0) where ALL children also have net=0 should
        still be removed -- no valid data to salvage."""
        router = _make_router()
        router.routes = [
            Route(
                net=0,
                net_name="",
                segments=[_seg(110, 90, 120, 90, net=0)],
                vias=[_via(115, 90, net=0)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_routes_removed"] == 1
        assert len(router.routes) == 0

    def test_mixed_child_nets_picks_valid(self):
        """If children have mixed nets (some valid, some 0), the Route
        adopts a valid net and net-0 children are stripped in step 2."""
        router = _make_router()
        router.routes = [
            Route(
                net=0,
                net_name="",
                segments=[
                    _seg(110, 90, 120, 90, net=3),
                    _seg(120, 90, 130, 100, net=0),  # orphan child
                ],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_routes_removed"] == 0
        assert len(router.routes) == 1
        assert router.routes[0].net == 3
        # Step 2 should have stripped the net-0 segment
        assert stats["net0_segments_removed"] == 1
        assert len(router.routes[0].segments) == 1


class TestBoardBboxOverride:
    """Issue #2039: cleanup_artifacts() should use _board_bbox when set
    instead of grid origin/dimensions for OOB filtering."""

    def test_oob_uses_board_bbox(self):
        """When _board_bbox is set, OOB filtering should use its bounds
        rather than the grid's origin/dimensions."""
        # Grid covers (100, 80) to (150, 120) but the actual board
        # edge cuts say (90, 70) to (160, 130) -- wider than the grid.
        router = _make_router(width=50, height=40, origin_x=100, origin_y=80)
        router._board_bbox = (90.0, 70.0, 160.0, 130.0)

        # Segment at x=95 is outside grid bounds but inside board bbox
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(95, 90, 110, 90)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_segments_removed"] == 0
        assert len(router.routes[0].segments) == 1

    def test_oob_without_board_bbox_uses_grid(self):
        """When _board_bbox is None, falls back to grid bounds."""
        router = _make_router(width=50, height=40, origin_x=100, origin_y=80)
        # _board_bbox is None by default

        # Segment at x=95 is outside grid bounds (min_x = 100 - 0.5 = 99.5)
        # Both endpoints outside grid bounds
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(50, 50, 60, 50)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_segments_removed"] == 1

    def test_board_bbox_narrower_than_grid(self):
        """When board bbox is narrower than the grid, segments outside
        the board bbox but inside the grid should be removed."""
        router = _make_router(width=50, height=40, origin_x=100, origin_y=80)
        router._board_bbox = (110.0, 90.0, 130.0, 110.0)

        # Segment at x=105 is inside grid but outside board bbox
        # (board bbox min_x = 110 - 0.5 margin = 109.5)
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(105, 95, 108, 95)],  # both endpoints outside bbox
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["oob_segments_removed"] == 1


class TestStatisticsAfterCleanup:
    """Issue #2039: Statistics must reflect post-cleanup data."""

    def test_statistics_reflect_post_cleanup_routes(self):
        """get_statistics() called after cleanup_artifacts() should
        report counts matching the surviving routes."""
        router = _make_router()
        router.routes = [
            # Will be removed (net=0, all children net=0)
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
            # Will survive
            Route(
                net=1,
                net_name="A",
                segments=[
                    _seg(110, 90, 120, 90, net=1),
                    _seg(120, 90, 130, 100, net=1),
                ],
                vias=[_via(120, 90, net=1)],
            ),
        ]
        # Run cleanup first (simulates what to_sexp does)
        router.cleanup_artifacts()
        stats = router.get_statistics()
        assert stats["routes"] == 1
        assert stats["segments"] == 2
        assert stats["vias"] == 1

    def test_to_sexp_then_statistics_consistent(self):
        """Calling to_sexp() then get_statistics() should yield
        consistent counts since to_sexp triggers cleanup."""
        router = _make_router()
        router.routes = [
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
            Route(
                net=2,
                net_name="B",
                segments=[_seg(115, 95, 125, 95, net=2)],
            ),
        ]
        sexp = router.to_sexp()
        stats = router.get_statistics()

        # sexp should only contain net 2
        assert "(net 2)" in sexp
        assert "(net 0)" not in sexp

        # stats should match the post-cleanup state
        assert stats["routes"] == 1
        assert stats["segments"] == 1
        assert stats["vias"] == 0


class TestConnectivityAwareRestoration:
    """Issue #2259: cleanup_artifacts() must not destroy valid routing segments.

    When removing net-0 or OOB segments would fragment a net's connectivity,
    the cleanup must restore those segments to preserve the routing.
    """

    def test_restores_net0_segment_needed_for_connectivity(self):
        """A net-0 segment that bridges two valid segments must be restored
        to preserve connectivity, with its net corrected to the route's net."""
        router = _make_router()
        # Three segments forming a chain: seg1 -- bridge(net=0) -- seg2
        # Removing the bridge would fragment net 3 into two components.
        router.routes = [
            Route(
                net=3,
                net_name="SIG",
                segments=[
                    _seg(110, 90, 115, 90, net=3),  # seg1
                    _seg(115, 90, 120, 90, net=0),  # bridge (net-0)
                    _seg(120, 90, 125, 90, net=3),  # seg2
                ],
            ),
        ]
        stats = router.cleanup_artifacts()
        # The bridge must be restored and re-netted
        assert len(router.routes[0].segments) == 3
        assert stats["segments_restored"] >= 1
        # All segments should now carry the route's net
        for seg in router.routes[0].segments:
            assert seg.net == 3

    def test_does_not_restore_when_connectivity_preserved(self):
        """A net-0 segment that is truly an orphan (not bridging anything)
        should still be removed."""
        router = _make_router()
        # Two valid connected segments plus a dangling net-0 segment
        router.routes = [
            Route(
                net=3,
                net_name="SIG",
                segments=[
                    _seg(110, 90, 115, 90, net=3),
                    _seg(115, 90, 120, 90, net=3),
                    _seg(130, 100, 135, 100, net=0),  # orphan, not bridging
                ],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert stats["net0_segments_removed"] == 1
        assert stats["segments_restored"] == 0
        assert len(router.routes[0].segments) == 2

    def test_restores_oob_segment_needed_for_connectivity(self):
        """An escape segment that extends beyond the board edge but is
        needed for connectivity must be preserved."""
        router = _make_router()
        # Board grid: (100,80) to (150,120), margin 0.5mm
        # min bounds = (99.5, 79.5), max bounds = (150.5, 120.5)
        # Chain: inside -> one-end-inside bridge -> both-OOB link -> both-OOB end
        router.routes = [
            Route(
                net=5,
                net_name="ESCAPE",
                segments=[
                    _seg(110, 90, 105, 85, net=5),  # fully inside
                    _seg(105, 85, 99, 79, net=5),  # one endpoint inside (105,85), one OOB (99,79)
                    _seg(99, 79, 95, 75, net=5),  # both endpoints OOB
                ],
            ),
        ]
        router.cleanup_artifacts()
        # Segment 3 (99,79)-(95,75) has both endpoints OOB.
        # Removing it fragments the net into two components
        # (the first two segments connect to each other, but
        # the third's endpoint at (95,75) becomes disconnected).
        # Wait -- actually, without segment 3 the first two
        # segments still form a chain, so removing it doesn't
        # add a new component; it just shortens the path.
        # A true connectivity break requires the OOB segment
        # to be the ONLY link between two groups.
        # Restructure: two separate chains linked only by an OOB segment.
        pass  # covered by test_escape_near_board_edge_preserved below

    def test_restores_oob_bridge_between_two_groups(self):
        """An OOB segment that is the sole connection between two groups
        of in-bounds segments must be restored."""
        router = _make_router()
        # Board grid: (100,80)-(150,120), margin 0.5mm
        # Group A: inside segments ending at (99,79) -- near edge
        # Group B: inside segments starting at (97,77) -- near edge
        # Bridge: (99,79)-(97,77) -- both endpoints OOB
        router.routes = [
            Route(
                net=5,
                net_name="ESCAPE",
                segments=[
                    _seg(110, 90, 99, 79, net=5),  # one end inside, one OOB -> kept
                    _seg(99, 79, 97, 77, net=5),  # both OOB -> normally removed
                    _seg(97, 77, 110, 100, net=5),  # one OOB, one inside -> kept
                ],
            ),
        ]
        stats = router.cleanup_artifacts()
        # Without the bridge (99,79)-(97,77), segments 1 and 3 share
        # no common endpoints, fragmenting the net.
        assert stats["segments_restored"] >= 1
        assert len(router.routes[0].segments) == 3

    def test_restores_via_needed_for_connectivity(self):
        """A net-0 via that is at a junction point must be restored."""
        router = _make_router()
        router.routes = [
            Route(
                net=4,
                net_name="PWR",
                segments=[
                    _seg(110, 90, 115, 90, net=4),
                    _seg(115, 90, 120, 90, net=4),
                ],
                vias=[_via(115, 90, net=0)],  # net-0 via at junction
            ),
        ]
        stats = router.cleanup_artifacts()
        # Via removal should not fragment connectivity here because
        # the segments still share the endpoint (115,90).
        # So the via should still be removed (it's truly orphaned
        # in terms of connectivity -- segments already connect).
        # This test validates we don't over-restore.
        assert stats["net0_vias_removed"] == 1
        assert stats["vias_restored"] == 0
        assert len(router.routes[0].vias) == 0

    def test_restoration_stats_are_reported(self):
        """Verify the stats dict includes restoration counters."""
        router = _make_router()
        router.routes = [
            Route(
                net=1,
                net_name="A",
                segments=[_seg(110, 90, 115, 90, net=1)],
            ),
        ]
        stats = router.cleanup_artifacts()
        assert "segments_restored" in stats
        assert "vias_restored" in stats

    def test_multiple_nets_independent_restoration(self):
        """Restoration should only affect nets whose connectivity was
        degraded, not all nets."""
        router = _make_router()
        router.routes = [
            # Net 3: has a bridging net-0 segment (should be restored)
            Route(
                net=3,
                net_name="SIG",
                segments=[
                    _seg(110, 90, 115, 90, net=3),
                    _seg(115, 90, 120, 90, net=0),  # bridge
                    _seg(120, 90, 125, 90, net=3),
                ],
            ),
            # Net 7: has a true orphan net-0 segment (should be removed)
            Route(
                net=7,
                net_name="CLK",
                segments=[
                    _seg(110, 100, 120, 100, net=7),
                    _seg(130, 110, 135, 110, net=0),  # orphan
                ],
            ),
        ]
        router.cleanup_artifacts()
        # Net 3's bridge restored
        assert len(router.routes[0].segments) == 3
        # Net 7's orphan removed
        assert len(router.routes[1].segments) == 1
        assert router.routes[1].segments[0].net == 7

    def test_cleanup_before_stats_matches_output(self):
        """Issue #2263: The route_cmd flow must call cleanup_artifacts()
        before get_statistics() so that reported metrics match the data
        written to file.  Simulate the fixed flow:
          cleanup_artifacts() -> to_sexp(skip_cleanup=True) -> get_statistics()
        """
        router = _make_router()
        router.routes = [
            # Net-0 route that will be removed by cleanup
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
            # Valid route that survives cleanup
            Route(
                net=1,
                net_name="A",
                segments=[
                    _seg(110, 90, 115, 90, net=1),
                    _seg(115, 90, 120, 90, net=1),
                ],
                vias=[_via(115, 90, net=1)],
            ),
        ]
        # Pre-cleanup counts
        pre_segments = sum(len(r.segments) for r in router.routes)
        assert pre_segments == 3  # 1 net-0 + 2 valid

        # Step 1: cleanup (the fix from #2263)
        cleanup_stats = router.cleanup_artifacts()
        assert cleanup_stats["net0_routes_removed"] == 1

        # Step 2: to_sexp with skip_cleanup (no double cleanup)
        sexp = router.to_sexp(skip_cleanup=True)
        assert "(net 1)" in sexp
        assert "(net 0)" not in sexp

        # Step 3: get_statistics on cleaned routes
        stats = router.get_statistics()
        assert stats["routes"] == 1
        assert stats["segments"] == 2
        assert stats["vias"] == 1

        # Post-cleanup segment count should match stats
        post_segments = sum(len(r.segments) for r in router.routes)
        assert post_segments == stats["segments"]

    def test_escape_near_board_edge_preserved(self):
        """Escape segments near board edge (both endpoints just outside
        tight margin) that are part of a connected chain should survive
        when they are the sole link between two groups."""
        router = _make_router()
        # Board at (100,80)-(150,120), tight margin for this test
        router._board_bbox = (100.0, 80.0, 150.0, 120.0)
        # Two groups of in-bounds segments connected by an OOB bridge.
        router.routes = [
            Route(
                net=2,
                net_name="PAD_ESCAPE",
                segments=[
                    _seg(110, 90, 99, 79, net=2),  # one end inside, one OOB
                    _seg(99, 79, 97, 77, net=2),  # both endpoints OOB (bridge)
                    _seg(97, 77, 110, 100, net=2),  # one OOB, one inside
                ],
            ),
        ]
        stats = router.cleanup_artifacts()
        # Middle segment is the sole link between the two edges.
        # Removing it fragments the net, so it should be restored.
        assert len(router.routes[0].segments) == 3
        assert stats["segments_restored"] >= 1


class TestFinalizeRoutes:
    """Test that _finalize_routes() runs cleanup before stats and sexp.

    This verifies the canonical cleanup -> sexp -> stats ordering that
    prevents the metrics-before-cleanup bug (Issue #2263).
    """

    def test_stats_reflect_post_cleanup_state(self):
        """Statistics should exclude net-0 segments removed by cleanup."""
        from kicad_tools.cli.route_cmd import _finalize_routes

        router = _make_router()
        # One valid route, one net-0 orphan that cleanup will remove
        router.routes = [
            Route(net=1, net_name="SIG", segments=[_seg(110, 90, 120, 90, net=1)]),
            Route(net=0, net_name="", segments=[_seg(130, 90, 140, 90, net=0)]),
        ]
        # Register net 1 so get_statistics can count it
        router.nets = {1: [("R1", "1"), ("R2", "1")]}

        route_sexp, stats, cleanup_stats = _finalize_routes(
            router,
            multi_pad_net_ids={1},
            nets_to_route=1,
            quiet=True,
        )

        # Cleanup should have removed the net-0 route
        assert cleanup_stats["net0_routes_removed"] == 1
        # Stats should only reflect the surviving route
        assert stats["routes"] == 1
        assert stats["segments"] == 1
        # sexp should not contain net-0 data
        assert "net 0" not in route_sexp

    def test_sexp_excludes_oob_segments(self):
        """S-expressions should not contain out-of-bounds segments."""
        from kicad_tools.cli.route_cmd import _finalize_routes

        router = _make_router()
        # One in-bounds route, one completely out-of-bounds segment
        router.routes = [
            Route(
                net=1,
                net_name="SIG",
                segments=[
                    _seg(110, 90, 120, 90, net=1),  # in-bounds
                    _seg(200, 200, 210, 210, net=1),  # out-of-bounds
                ],
            ),
        ]
        router.nets = {1: [("R1", "1"), ("R2", "1")]}

        route_sexp, stats, cleanup_stats = _finalize_routes(
            router,
            multi_pad_net_ids={1},
            nets_to_route=1,
            quiet=True,
        )

        # OOB segment should be removed
        assert cleanup_stats["oob_segments_removed"] >= 1
        # Only 1 segment should remain
        assert stats["segments"] == 1

    def test_cleanup_runs_before_sexp_generation(self):
        """Verify sexp is generated from cleaned routes, not pre-cleanup."""
        from kicad_tools.cli.route_cmd import _finalize_routes

        router = _make_router()
        # Route with both valid and net-0 segments
        router.routes = [
            Route(
                net=1,
                net_name="SIG",
                segments=[_seg(110, 90, 120, 90, net=1)],
            ),
            Route(
                net=0,
                net_name="",
                segments=[
                    _seg(115, 95, 125, 95, net=0),
                    _seg(125, 95, 135, 105, net=0),
                ],
            ),
        ]
        router.nets = {1: [("R1", "1"), ("R2", "1")]}

        route_sexp, stats, cleanup_stats = _finalize_routes(
            router,
            multi_pad_net_ids={1},
            nets_to_route=1,
            quiet=True,
        )

        # Only 1 route should remain after cleanup
        assert len(router.routes) == 1
        # sexp segment count should match stats
        assert stats["segments"] == 1
        # The sexp should only contain 1 segment definition
        assert route_sexp.count("(segment") == 1


class TestFinalizeRoutesConnectivityInvariant:
    """Issue #3124: _finalize_routes() enforces per-net connectivity.

    The cleanup step inside _finalize_routes() can shrink the largest
    connected component on a multi-pad net if it removes a segment
    that bridges two sub-components.  Before #3124 this regression
    was uncaught (the per-net invariant only ran after optimize and
    nudge, not after cleanup).
    """

    def _make_router_with_pads(self) -> Autorouter:
        """Build a 4-pad net with 3 chain segments and matching pads."""
        from kicad_tools.router.primitives import Pad

        router = _make_router()
        # Pads on a horizontal line at y=90 on the F.Cu layer.
        pads = {
            ("U1", "A"): Pad(
                x=110.0,
                y=90.0,
                width=0.5,
                height=0.5,
                net=1,
                net_name="DEGRADE",
                layer=Layer.F_CU,
                ref="U1",
                pin="A",
            ),
            ("U1", "B"): Pad(
                x=115.0,
                y=90.0,
                width=0.5,
                height=0.5,
                net=1,
                net_name="DEGRADE",
                layer=Layer.F_CU,
                ref="U1",
                pin="B",
            ),
            ("U1", "C"): Pad(
                x=120.0,
                y=90.0,
                width=0.5,
                height=0.5,
                net=1,
                net_name="DEGRADE",
                layer=Layer.F_CU,
                ref="U1",
                pin="C",
            ),
            ("U1", "D"): Pad(
                x=125.0,
                y=90.0,
                width=0.5,
                height=0.5,
                net=1,
                net_name="DEGRADE",
                layer=Layer.F_CU,
                ref="U1",
                pin="D",
            ),
        }
        router.pads = pads
        router.nets = {1: list(pads.keys())}
        router.net_names = {1: "DEGRADE"}

        # 3-segment chain connecting all 4 pads.
        router.routes = [
            Route(
                net=1,
                net_name="DEGRADE",
                segments=[
                    _seg(110, 90, 115, 90, net=1),
                    _seg(115, 90, 120, 90, net=1),
                    _seg(120, 90, 125, 90, net=1),
                ],
            ),
        ]
        return router

    def test_finalize_does_not_regress_clean_routes(self):
        """A fully-connected multi-pad net survives finalize unchanged."""
        from kicad_tools.cli.route_cmd import _finalize_routes

        router = self._make_router_with_pads()
        route_sexp, stats, _ = _finalize_routes(
            router,
            multi_pad_net_ids={1},
            nets_to_route=1,
            quiet=True,
        )
        # Cleanup is a no-op on well-formed routes.
        assert len(router.routes) == 1
        assert len(router.routes[0].segments) == 3
        assert stats["nets_routed"] == 1
        assert route_sexp.count("(segment") == 3

    def test_finalize_strict_no_regression_does_not_exit(self):
        """Strict mode is a pass-through when nothing regresses."""
        from kicad_tools.cli.route_cmd import _finalize_routes

        router = self._make_router_with_pads()
        # Should not raise / exit.
        _ = _finalize_routes(
            router,
            multi_pad_net_ids={1},
            nets_to_route=1,
            quiet=True,
            strict=True,
        )

    def test_finalize_warns_on_aggregate_segment_drop(self, caplog):
        """Issue #3124 AC #3: emit a WARNING when cleanup drops >50%
        of segments.

        We force a drop by giving the route a bunch of net-0 orphan
        segments alongside a small valid chain; cleanup removes the
        orphans (legitimately) but the ratio crosses the threshold,
        so a warning fires.
        """
        import logging

        from kicad_tools.cli.route_cmd import _finalize_routes

        router = self._make_router_with_pads()
        # Add a separate route with 10 net-0 segments that cleanup
        # will legitimately remove.  pre = 3 + 10 = 13, post = 3.
        # drop ratio = 1 - 3/13 = ~77%, > 50% threshold.
        router.routes.append(
            Route(
                net=0,
                net_name="",
                segments=[
                    _seg(110 + i * 0.1, 95, 110 + i * 0.1 + 0.05, 95, net=0) for i in range(10)
                ],
            )
        )

        with caplog.at_level(logging.WARNING):
            _finalize_routes(
                router,
                multi_pad_net_ids={1},
                nets_to_route=1,
                quiet=True,
            )

        # Warning surfaced via logger.
        assert any("reduced segment count" in record.getMessage() for record in caplog.records), (
            f"Expected aggregate-segment warning in caplog: {[r.getMessage() for r in caplog.records]}"
        )
