"""Tests for routing connectivity validation (Issue #2254).

Verifies that the router correctly reports actual pad-to-pad connectivity
rather than inflated counts based on segment existence alone.
"""

from kicad_tools.router.fine_pitch import (
    FinePitchReport,
    FinePitchSeverity,
    analyze_fine_pitch_components,
)
from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.primitives import Layer, Pad, Route, Segment


def _pad(x: float, y: float, net: int, ref: str = "U1", pin: str = "1") -> Pad:
    """Create a minimal Pad for testing."""
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=f"NET{net}",
        ref=ref,
        pin=pin,
    )


def _seg(x1: float, y1: float, x2: float, y2: float) -> Segment:
    """Create a minimal Segment for testing."""
    return Segment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=0.2,
        layer=Layer.F_CU,
    )


class TestValidateNetConnectivity:
    """Tests for validate_net_connectivity()."""

    def test_fully_connected_two_pad_net(self):
        """A net with two pads connected by a single segment is fully connected."""
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]
        routes = [Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 5.0, 0.0)])]

        result = validate_net_connectivity(routes, {1: pads})

        assert result[1]["total_pads"] == 2
        assert result[1]["connected_pads"] == 2
        assert result[1]["connected"] is True

    def test_disconnected_escape_stubs(self):
        """Two escape stubs that don't connect produce disconnected islands."""
        pads = [
            _pad(0.0, 0.0, 1, "U1", "1"),
            _pad(10.0, 0.0, 1, "U1", "2"),
            _pad(20.0, 0.0, 1, "U1", "3"),
        ]
        # Two short escape stubs near pad 1 and pad 2, but no connection between
        routes = [
            Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 1.0, 0.0)]),
            Route(net=1, net_name="NET1", segments=[_seg(10.0, 0.0, 11.0, 0.0)]),
        ]

        result = validate_net_connectivity(routes, {1: pads})

        assert result[1]["total_pads"] == 3
        assert result[1]["connected_pads"] < 3
        assert result[1]["connected"] is False

    def test_no_routes_for_net(self):
        """A net with no routes at all reports 0 connected pads."""
        pads = [_pad(0.0, 0.0, 1), _pad(5.0, 0.0, 1)]

        result = validate_net_connectivity([], {1: pads})

        assert result[1]["total_pads"] == 2
        assert result[1]["connected_pads"] == 0
        assert result[1]["connected"] is False

    def test_single_pad_net_is_trivially_connected(self):
        """A single-pad net is always connected."""
        pads = [_pad(0.0, 0.0, 1)]

        result = validate_net_connectivity([], {1: pads})

        assert result[1]["total_pads"] == 1
        assert result[1]["connected_pads"] == 1
        assert result[1]["connected"] is True
        assert result[1]["stranded_pads"] == []

    def test_stranded_pads_reported_for_partial_net(self):
        """A partial net lists the pad identifiers that are not connected (#4316)."""
        pads = [
            _pad(0.0, 0.0, 1, "U1", "1"),
            _pad(5.0, 0.0, 1, "U2", "2"),
            _pad(20.0, 20.0, 1, "R3", "1"),
        ]
        # Segment connects U1.1 <-> U2.2 only; R3.1 is far away and stranded.
        routes = [Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 5.0, 0.0)])]

        result = validate_net_connectivity(routes, {1: pads})

        assert result[1]["connected_pads"] == 2
        assert result[1]["connected"] is False
        assert result[1]["stranded_pads"] == ["R3.1"]

    def test_stranded_pads_lists_all_pads_when_no_routes(self):
        """When a net has no routes, every pad is stranded (#4316)."""
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U2", "2")]

        result = validate_net_connectivity([], {1: pads})

        assert sorted(result[1]["stranded_pads"]) == ["U1.1", "U2.2"]

    def test_stranded_pads_empty_when_fully_connected(self):
        """A fully connected net has no stranded pads (#4316)."""
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U2", "2")]
        routes = [Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 5.0, 0.0)])]

        result = validate_net_connectivity(routes, {1: pads})

        assert result[1]["connected"] is True
        assert result[1]["stranded_pads"] == []

    def test_chain_of_segments_connects_all_pads(self):
        """Three pads connected by a chain of segments are fully connected."""
        pads = [
            _pad(0.0, 0.0, 1, "U1", "1"),
            _pad(5.0, 0.0, 1, "U1", "2"),
            _pad(10.0, 0.0, 1, "U1", "3"),
        ]
        routes = [
            Route(
                net=1,
                net_name="NET1",
                segments=[_seg(0.0, 0.0, 5.0, 0.0), _seg(5.0, 0.0, 10.0, 0.0)],
            ),
        ]

        result = validate_net_connectivity(routes, {1: pads})

        assert result[1]["total_pads"] == 3
        assert result[1]["connected_pads"] == 3
        assert result[1]["connected"] is True

    def test_multiple_nets_independent(self):
        """Connectivity is validated per-net independently."""
        net1_pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]
        net2_pads = [_pad(0.0, 5.0, 2, "U1", "3"), _pad(5.0, 5.0, 2, "U1", "4")]

        routes = [
            Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 5.0, 0.0)]),
            # Net 2 has no routes
        ]

        result = validate_net_connectivity(routes, {1: net1_pads, 2: net2_pads})

        assert result[1]["connected"] is True
        assert result[2]["connected"] is False

    def test_pad_near_segment_endpoint_linked(self):
        """A pad close to (but not exactly at) a segment endpoint is linked."""
        # Pad at 0.005mm offset from segment endpoint (within default tolerance)
        pads = [_pad(0.005, 0.005, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]
        routes = [Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 5.0, 0.0)])]

        result = validate_net_connectivity(routes, {1: pads})

        assert result[1]["connected_pads"] == 2
        assert result[1]["connected"] is True


class TestComputeRoutingStatisticsConnectivity:
    """Tests for connectivity-aware compute_routing_statistics()."""

    def test_nets_routed_reflects_connectivity(self):
        """nets_routed should only count nets where all pads are connected."""
        from unittest.mock import MagicMock

        from kicad_tools.router.observability import compute_routing_statistics

        grid = MagicMock()
        grid.get_congestion_map.return_value = {
            "max_congestion": 0,
            "avg_congestion": 0,
            "congested_regions": 0,
        }

        pads_net1 = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]
        pads_net2 = [_pad(0.0, 5.0, 2, "U1", "3"), _pad(5.0, 5.0, 2, "U1", "4")]

        routes = [
            # Net 1: fully connected
            Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 5.0, 0.0)]),
            # Net 2: only an escape stub (not connected to pad 4)
            Route(net=2, net_name="NET2", segments=[_seg(0.0, 5.0, 1.0, 5.0)]),
        ]

        stats = compute_routing_statistics(
            routes=routes,
            grid=grid,
            layer_stats={},
            net_pads={1: pads_net1, 2: pads_net2},
        )

        # Net 1 is fully connected, net 2 is not
        assert stats["nets_routed"] == 1
        assert stats["has_disconnected_islands"] is True
        assert stats["nets_fully_connected"] == 1

    def test_legacy_path_without_net_pads(self):
        """Without net_pads, nets_routed counts any net with a route."""
        from unittest.mock import MagicMock

        from kicad_tools.router.observability import compute_routing_statistics

        grid = MagicMock()
        grid.get_congestion_map.return_value = {
            "max_congestion": 0,
            "avg_congestion": 0,
            "congested_regions": 0,
        }

        routes = [
            Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 1.0, 0.0)]),
            Route(net=2, net_name="NET2", segments=[_seg(0.0, 5.0, 1.0, 5.0)]),
        ]

        stats = compute_routing_statistics(
            routes=routes,
            grid=grid,
            layer_stats={},
            net_pads=None,
        )

        # Legacy: counts any net with a route
        assert stats["nets_routed"] == 2
        assert "connectivity" not in stats


class TestOffGridWarning:
    """Tests for the >50% off-grid warning (acceptance criterion 3)."""

    def test_no_warning_below_threshold(self):
        """No off-grid warning when percentage is below 50%."""
        report = FinePitchReport(
            total_pads=10,
            total_off_grid=4,
            grid_resolution=0.065,
        )
        # 40% off-grid -- below threshold
        assert report.off_grid_percentage == 40.0
        # has_warnings is False because no components have issues
        text = report.format_warnings()
        assert "WARNING" not in text

    def test_warning_above_threshold(self):
        """Warning emitted when >50% of pads are off-grid."""
        from kicad_tools.router.fine_pitch import ComponentGridAnalysis

        comp = ComponentGridAnalysis(
            ref="U1",
            package_type="TSSOP-20",
            pin_count=20,
            pin_pitch=0.65,
            off_grid_count=18,
            off_grid_percentage=90.0,
            severity=FinePitchSeverity.CRITICAL,
            recommendations=["Use 0.025mm grid"],
        )
        report = FinePitchReport(
            components=[comp],
            total_pads=20,
            total_off_grid=18,
            grid_resolution=0.065,
        )

        assert report.off_grid_percentage == 90.0
        text = report.format_warnings()
        assert "WARNING" in text
        assert "90%" in text
        assert "off-grid" in text

    def test_off_grid_percentage_property(self):
        """off_grid_percentage computed correctly."""
        report = FinePitchReport(total_pads=100, total_off_grid=87)
        assert report.off_grid_percentage == 87.0

    def test_off_grid_percentage_zero_pads(self):
        """off_grid_percentage is 0 when no pads exist."""
        report = FinePitchReport(total_pads=0, total_off_grid=0)
        assert report.off_grid_percentage == 0.0

    def test_analyze_off_grid_board_warns(self):
        """End-to-end: analyzing a mostly off-grid board produces a warning."""
        # Create 10 pads at 0.65mm pitch on a 0.5mm grid
        pads = {}
        for i in range(10):
            x = i * 0.65
            pads[("U1", str(i + 1))] = Pad(
                x=x,
                y=0.0,
                width=0.3,
                height=0.8,
                net=i + 1,
                net_name=f"NET{i + 1}",
                ref="U1",
                pin=str(i + 1),
            )

        report = analyze_fine_pitch_components(
            pads=pads,
            grid_resolution=0.5,
            trace_width=0.2,
            clearance=0.2,
        )

        # Most pads should be off-grid at 0.5mm resolution
        assert report.off_grid_percentage > 50
        text = report.format_warnings()
        assert "WARNING" in text


class TestShowRoutingSummaryHonesty:
    """Tests that the routing summary does not mislead when islands exist."""

    def test_incomplete_banner_with_islands(self, capsys):
        """show_routing_summary prints 'Routing Incomplete' when islands exist."""
        from unittest.mock import MagicMock

        from kicad_tools.router.output import show_routing_summary

        router = MagicMock()
        # Net 1 has routes but pads are disconnected
        router.routes = [
            Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 1.0, 0.0)]),
        ]
        router.routing_failures = []
        router.pads = {
            ("U1", "1"): _pad(0.0, 0.0, 1, "U1", "1"),
            ("U1", "2"): _pad(10.0, 0.0, 1, "U1", "2"),
        }
        router.nets = {1: [("U1", "1"), ("U1", "2")]}

        net_map = {"NET1": 1}

        show_routing_summary(
            router=router,
            net_map=net_map,
            nets_to_route=1,
            nets_to_route_ids={1},
        )

        captured = capsys.readouterr()
        assert "Routing Incomplete" in captured.out
        assert "Routing Complete!" not in captured.out

    def test_complete_banner_when_fully_connected(self, capsys):
        """show_routing_summary prints 'Routing Complete!' when all nets connected."""
        from unittest.mock import MagicMock

        from kicad_tools.router.output import show_routing_summary

        router = MagicMock()
        router.routes = [
            Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 5.0, 0.0)]),
        ]
        router.routing_failures = []
        router.pads = {
            ("U1", "1"): _pad(0.0, 0.0, 1, "U1", "1"),
            ("U1", "2"): _pad(5.0, 0.0, 1, "U1", "2"),
        }
        router.nets = {1: [("U1", "1"), ("U1", "2")]}
        router._cleanup_stats = None

        net_map = {"NET1": 1}

        show_routing_summary(
            router=router,
            net_map=net_map,
            nets_to_route=1,
            nets_to_route_ids={1},
        )

        captured = capsys.readouterr()
        assert "Routing Complete!" in captured.out

    def test_partially_connected_nets_listed(self, capsys):
        """show_routing_summary lists partially-connected nets with pad counts."""
        from unittest.mock import MagicMock

        from kicad_tools.router.output import show_routing_summary

        router = MagicMock()
        # Net 1: segment near pad 1 only (escape stub)
        router.routes = [
            Route(net=1, net_name="SCL", segments=[_seg(0.0, 0.0, 1.0, 0.0)]),
        ]
        router.routing_failures = []
        router.pads = {
            ("U1", "1"): _pad(0.0, 0.0, 1, "U1", "1"),
            ("U2", "3"): _pad(20.0, 0.0, 1, "U2", "3"),
            ("U3", "5"): _pad(40.0, 0.0, 1, "U3", "5"),
        }
        router.nets = {1: [("U1", "1"), ("U2", "3"), ("U3", "5")]}
        router._cleanup_stats = None

        net_map = {"SCL": 1}

        show_routing_summary(
            router=router,
            net_map=net_map,
            nets_to_route=1,
            nets_to_route_ids={1},
        )

        captured = capsys.readouterr()
        # Should show per-net pad connectivity
        assert "1/3 pads connected" in captured.out
        assert "SCL" in captured.out
