"""Tests for post-route artifact cleanup (Issue #1979).

Tests the ``cleanup_artifacts()`` method on ``Autorouter`` which removes:
- Routes and segments/vias with net == 0
- Segments with both endpoints outside the board bounding box
- Vias with center outside the board bounding box
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
            # Net-0 route (removed entirely)
            Route(net=0, net_name="", segments=[_seg(110, 90, 120, 90, net=0)]),
            # Valid route with a net-0 segment and an OOB segment
            Route(
                net=3,
                net_name="MIX",
                segments=[
                    _seg(110, 90, 120, 90, net=3),   # valid
                    _seg(115, 95, 125, 95, net=0),    # net-0 orphan
                    _seg(200, 200, 210, 200, net=3),  # OOB
                ],
                vias=[
                    _via(115, 90, net=3),   # valid
                    _via(300, 300, net=3),   # OOB
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
