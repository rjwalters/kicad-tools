"""Tests for routability analysis module."""

import pytest

from kicad_tools.router import (
    Autorouter,
    BlockingObstacle,
    CongestionZone,
    DesignRules,
    NetRoutabilityReport,
    ObstacleType,
    RoutabilityAnalyzer,
    RoutabilityReport,
    RouteAlternative,
    RoutingFailureDiagnostic,
    RoutingSeverity,
    analyze_routing_failure,
)


class TestObstacleType:
    """Tests for ObstacleType enum."""

    def test_obstacle_types_exist(self):
        assert ObstacleType.PAD
        assert ObstacleType.TRACE
        assert ObstacleType.VIA
        assert ObstacleType.ZONE
        assert ObstacleType.COMPONENT
        assert ObstacleType.KEEPOUT
        assert ObstacleType.BOARD_EDGE


class TestRoutingSeverity:
    """Tests for RoutingSeverity enum."""

    def test_severity_levels_exist(self):
        assert RoutingSeverity.LOW
        assert RoutingSeverity.MEDIUM
        assert RoutingSeverity.HIGH
        assert RoutingSeverity.CRITICAL


class TestBlockingObstacle:
    """Tests for BlockingObstacle dataclass."""

    def test_blocking_obstacle_creation(self):
        obs = BlockingObstacle(
            obstacle_type=ObstacleType.PAD,
            x=10.0,
            y=20.0,
            width=1.0,
            height=1.0,
            net=5,
            net_name="VCC",
            ref="U1",
            layer="F.Cu",
        )
        assert obs.obstacle_type == ObstacleType.PAD
        assert obs.x == 10.0
        assert obs.y == 20.0
        assert obs.net == 5
        assert obs.net_name == "VCC"
        assert obs.ref == "U1"

    def test_blocking_obstacle_position(self):
        obs = BlockingObstacle(
            obstacle_type=ObstacleType.TRACE,
            x=15.5,
            y=25.5,
            width=0.5,
            height=0.5,
        )
        assert obs.position == (15.5, 25.5)

    def test_blocking_obstacle_str_with_ref(self):
        obs = BlockingObstacle(
            obstacle_type=ObstacleType.PAD,
            x=10.0,
            y=20.0,
            width=1.0,
            height=1.0,
            ref="U1",
        )
        assert "PAD" in str(obs)
        assert "U1" in str(obs)

    def test_blocking_obstacle_str_with_net_name(self):
        obs = BlockingObstacle(
            obstacle_type=ObstacleType.TRACE,
            x=10.0,
            y=20.0,
            width=0.5,
            height=0.5,
            net_name="SDA",
        )
        assert "TRACE" in str(obs)
        assert "SDA" in str(obs)

    def test_blocking_obstacle_str_minimal(self):
        obs = BlockingObstacle(
            obstacle_type=ObstacleType.ZONE,
            x=5.0,
            y=5.0,
            width=10.0,
            height=10.0,
        )
        assert "ZONE" in str(obs)
        assert "5.00" in str(obs)


class TestRouteAlternative:
    """Tests for RouteAlternative dataclass."""

    def test_route_alternative_basic(self):
        alt = RouteAlternative(
            description="Route around obstacle",
            extra_length_mm=3.5,
        )
        assert alt.description == "Route around obstacle"
        assert alt.extra_length_mm == 3.5
        assert alt.feasible is True

    def test_route_alternative_with_vias(self):
        alt = RouteAlternative(
            description="Route on different layer",
            via_count=2,
        )
        assert alt.via_count == 2
        assert "+2 via" in str(alt).lower() or "2 via" in str(alt).lower()

    def test_route_alternative_not_feasible(self):
        alt = RouteAlternative(
            description="Direct route",
            feasible=False,
            reason="Blocked by component",
        )
        assert alt.feasible is False
        assert "[X]" in str(alt)
        assert "Blocked" in str(alt)


class TestRoutingFailureDiagnostic:
    """Tests for RoutingFailureDiagnostic dataclass."""

    def test_failure_diagnostic_creation(self):
        diag = RoutingFailureDiagnostic(
            net=5,
            net_name="SDA",
            source_pad=("U1", "3"),
            source_position=(10.0, 20.0),
            target_pad=("U2", "5"),
            target_position=(50.0, 30.0),
            straight_line_distance=41.2,
        )
        assert diag.net == 5
        assert diag.net_name == "SDA"
        assert diag.source_pad == ("U1", "3")
        assert diag.target_pad == ("U2", "5")

    def test_failure_diagnostic_severity_no_obstacles(self):
        diag = RoutingFailureDiagnostic(
            net=1,
            net_name="NET1",
            source_pad=("R1", "1"),
            source_position=(0, 0),
            target_pad=("R1", "2"),
            target_position=(5, 0),
            straight_line_distance=5.0,
        )
        assert diag.severity == RoutingSeverity.MEDIUM

    def test_failure_diagnostic_severity_multiple_obstacles(self):
        diag = RoutingFailureDiagnostic(
            net=1,
            net_name="NET1",
            source_pad=("R1", "1"),
            source_position=(0, 0),
            target_pad=("R1", "2"),
            target_position=(5, 0),
            straight_line_distance=5.0,
            blocking_obstacles=[
                BlockingObstacle(ObstacleType.PAD, 1, 0, 1, 1),
                BlockingObstacle(ObstacleType.PAD, 2, 0, 1, 1),
                BlockingObstacle(ObstacleType.PAD, 3, 0, 1, 1),
            ],
        )
        assert diag.severity == RoutingSeverity.CRITICAL


class TestCongestionZone:
    """Tests for CongestionZone dataclass."""

    def test_congestion_zone_creation(self):
        zone = CongestionZone(
            x=10.0,
            y=20.0,
            width=5.0,
            height=5.0,
            layer=0,
            density=0.75,
            competing_nets=5,
            available_channels=3,
        )
        assert zone.density == 0.75
        assert zone.competing_nets == 5
        assert zone.available_channels == 3

    def test_congestion_zone_is_bottleneck(self):
        zone = CongestionZone(
            x=10.0,
            y=20.0,
            width=5.0,
            height=5.0,
            layer=0,
            density=0.9,
            competing_nets=8,
            available_channels=2,
        )
        assert zone.is_bottleneck is True

    def test_congestion_zone_not_bottleneck(self):
        zone = CongestionZone(
            x=10.0,
            y=20.0,
            width=5.0,
            height=5.0,
            layer=0,
            density=0.5,
            competing_nets=2,
            available_channels=4,
        )
        assert zone.is_bottleneck is False


class TestNetRoutabilityReport:
    """Tests for NetRoutabilityReport dataclass."""

    def test_net_report_creation(self):
        report = NetRoutabilityReport(
            net=5,
            net_name="CLK",
            pad_count=4,
            pads=[("U1", "1"), ("U2", "2"), ("U3", "3"), ("U4", "4")],
            total_manhattan_distance=50.0,
            estimated_route_length=60.0,
        )
        assert report.net == 5
        assert report.net_name == "CLK"
        assert report.pad_count == 4

    def test_net_report_difficulty_score_low(self):
        report = NetRoutabilityReport(
            net=1,
            net_name="NET1",
            pad_count=2,
            pads=[("R1", "1"), ("R1", "2")],
            total_manhattan_distance=5.0,
            estimated_route_length=6.0,
            blocking_obstacles=[],
            congestion_zones=[],
        )
        assert report.difficulty_score == 0.0

    def test_net_report_difficulty_score_high(self):
        report = NetRoutabilityReport(
            net=1,
            net_name="NET1",
            pad_count=2,
            pads=[("R1", "1"), ("R1", "2")],
            total_manhattan_distance=5.0,
            estimated_route_length=6.0,
            blocking_obstacles=[
                BlockingObstacle(ObstacleType.PAD, 1, 0, 1, 1),
                BlockingObstacle(ObstacleType.PAD, 2, 0, 1, 1),
            ],
        )
        assert report.difficulty_score > 20


class TestRoutabilityReport:
    """Tests for RoutabilityReport dataclass."""

    def test_routability_report_creation(self):
        report = RoutabilityReport()
        assert report.total_nets == 0
        assert report.expected_routable == 0
        assert report.estimated_success_rate == 1.0

    def test_routability_report_with_nets(self):
        net1 = NetRoutabilityReport(
            net=1,
            net_name="NET1",
            pad_count=2,
            pads=[],
            total_manhattan_distance=5.0,
            estimated_route_length=6.0,
            routable=True,
        )
        net2 = NetRoutabilityReport(
            net=2,
            net_name="NET2",
            pad_count=3,
            pads=[],
            total_manhattan_distance=10.0,
            estimated_route_length=12.0,
            routable=False,
        )
        report = RoutabilityReport(net_reports=[net1, net2])
        assert report.total_nets == 2
        assert report.expected_routable == 1


class TestRoutabilityAnalyzer:
    """Tests for RoutabilityAnalyzer class."""

    def test_analyzer_creation(self):
        rules = DesignRules()
        router = Autorouter(100.0, 100.0, rules=rules)
        analyzer = RoutabilityAnalyzer(router)
        assert analyzer.autorouter is router
        assert analyzer.grid is router.grid
        assert analyzer.rules is router.rules

    def test_analyzer_empty_board(self):
        rules = DesignRules()
        router = Autorouter(100.0, 100.0, rules=rules)
        analyzer = RoutabilityAnalyzer(router)
        report = analyzer.analyze()
        assert report.total_nets == 0
        assert report.estimated_success_rate == 1.0

    def test_analyzer_single_net(self):
        rules = DesignRules()
        router = Autorouter(100.0, 100.0, rules=rules)

        # Add a simple 2-pad net
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 20.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )

        analyzer = RoutabilityAnalyzer(router)
        report = analyzer.analyze()

        assert report.total_nets == 1
        assert len(report.net_reports) == 1
        assert report.net_reports[0].net_name == "NET1"

    def test_analyzer_with_blocking_component(self):
        rules = DesignRules()
        router = Autorouter(100.0, 100.0, rules=rules)

        # Add a net that needs to route past another component
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 50.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )

        # Add blocking component in between
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 30.0, "y": 10.0, "net": 2, "net_name": "NET2"},
            ],
        )

        analyzer = RoutabilityAnalyzer(router)
        report = analyzer.analyze()

        assert report.total_nets == 2  # NET1 and NET2

    def test_analyzer_layer_utilization(self):
        rules = DesignRules()
        router = Autorouter(50.0, 50.0, rules=rules)

        # Add some pads
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1},
                {"number": "2", "x": 20.0, "y": 20.0, "net": 2},
            ],
        )

        analyzer = RoutabilityAnalyzer(router)
        report = analyzer.analyze()

        assert "F.Cu" in report.layer_utilization
        assert "B.Cu" in report.layer_utilization

    def test_analyzer_recommendations(self):
        rules = DesignRules()
        router = Autorouter(100.0, 100.0, rules=rules)

        analyzer = RoutabilityAnalyzer(router)
        report = analyzer.analyze()

        # Empty board should get positive recommendation
        assert len(report.recommendations) > 0


class TestAnalyzeRoutingFailure:
    """Tests for analyze_routing_failure function."""

    def test_analyze_failure_basic(self):
        rules = DesignRules()
        router = Autorouter(100.0, 100.0, rules=rules)

        # Add two pads
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 50.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )

        source = router.pads[("R1", "1")]
        target = router.pads[("R1", "2")]

        diag = analyze_routing_failure(router, source, target, 1)

        assert diag.net == 1
        assert diag.net_name == "NET1"
        assert diag.straight_line_distance == pytest.approx(40.0, rel=0.1)

    def test_analyze_failure_with_blocking(self):
        rules = DesignRules()
        router = Autorouter(100.0, 100.0, rules=rules)

        # Add source/target
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 50.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )

        # Add blocking component in the path
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 30.0, "y": 10.0, "net": 2, "net_name": "VCC"},
            ],
        )

        source = router.pads[("R1", "1")]
        target = router.pads[("R1", "2")]

        diag = analyze_routing_failure(router, source, target, 1)

        # Should have found the blocking pad
        assert len(diag.blocking_obstacles) > 0 or len(diag.alternatives) > 0
