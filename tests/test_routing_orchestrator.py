"""Tests for routing orchestration layer."""

from unittest.mock import MagicMock

import pytest

from kicad_tools.router import (
    AlternativeStrategy,
    PerformanceStats,
    RoutingMetrics,
    RoutingOrchestrator,
    RoutingResult,
    RoutingStrategy,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _pad(x: float, y: float, net: int = 1, net_name: str = "NET1",
         width: float = 1.0, height: float = 1.0,
         ref: str = "U1", pin: str = "1") -> Pad:
    """Helper to create Pad objects with sensible defaults."""
    return Pad(
        x=x, y=y, width=width, height=height,
        net=net, net_name=net_name, ref=ref, pin=pin,
    )


@pytest.fixture
def mock_pcb():
    """Create a mock PCB object."""
    pcb = MagicMock()
    pcb.width = 65.0
    pcb.height = 56.0
    return pcb


@pytest.fixture
def design_rules():
    """Create standard design rules."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        grid_resolution=0.1,
    )


@pytest.fixture
def orchestrator(mock_pcb, design_rules):
    """Create a routing orchestrator instance."""
    return RoutingOrchestrator(
        pcb=mock_pcb,
        rules=design_rules,
        backend="cpu",
    )


class TestRoutingStrategy:
    """Test routing strategy enum."""

    def test_strategy_enum_values(self):
        """Verify all strategy enum values are defined."""
        assert RoutingStrategy.GLOBAL_WITH_REPAIR
        assert RoutingStrategy.ESCAPE_THEN_GLOBAL
        assert RoutingStrategy.HIERARCHICAL_DIFF_PAIR
        assert RoutingStrategy.SUBGRID_ADAPTIVE
        assert RoutingStrategy.VIA_CONFLICT_RESOLUTION
        assert RoutingStrategy.FULL_PIPELINE


class TestRoutingResult:
    """Test routing result data structures."""

    def test_routing_result_to_dict(self):
        """Verify RoutingResult serialization to dictionary."""
        result = RoutingResult(
            success=True,
            net="USB_D+",
            strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
            metrics=RoutingMetrics(
                total_length_mm=15.0,
                via_count=2,
                layer_changes=1,
            ),
            performance=PerformanceStats(
                total_time_ms=100.0,
                gpu_utilized=False,
                backend_type="cpu",
            ),
        )

        data = result.to_dict()

        assert data["success"] is True
        assert data["net"] == "USB_D+"
        assert data["strategy_used"] == "HIERARCHICAL_DIFF_PAIR"
        assert data["metrics"]["total_length_mm"] == 15.0
        assert data["metrics"]["via_count"] == 2
        assert data["performance"]["total_time_ms"] == 100.0
        assert data["performance"]["backend_type"] == "cpu"

    def test_routing_result_with_alternatives(self):
        """Verify alternative strategies are serialized correctly."""
        result = RoutingResult(
            success=False,
            net="GND",
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            error_message="Routing failed due to congestion",
            alternative_strategies=[
                AlternativeStrategy(
                    strategy=RoutingStrategy.FULL_PIPELINE,
                    reason="Use complete pipeline for difficult nets",
                    estimated_cost=2.0,
                    success_probability=0.7,
                )
            ],
        )

        data = result.to_dict()

        assert data["success"] is False
        assert data["error_message"] == "Routing failed due to congestion"
        assert len(data["alternative_strategies"]) == 1
        assert data["alternative_strategies"][0]["strategy"] == "FULL_PIPELINE"
        assert data["alternative_strategies"][0]["success_probability"] == 0.7


class TestRoutingOrchestrator:
    """Test routing orchestrator functionality."""

    def test_orchestrator_initialization(self, orchestrator):
        """Verify orchestrator initializes with correct parameters."""
        assert orchestrator.backend == "cpu"
        assert orchestrator.corridor_width == 0.5
        assert orchestrator.density_threshold == 0.7
        assert orchestrator.enable_repair is True
        assert orchestrator.enable_via_conflict_resolution is True

    def test_strategy_selection_default(self, orchestrator):
        """Verify default strategy selection for standard nets."""
        # Create simple pads (wide spacing, not fine-pitch)
        pads = [
            _pad(x=0, y=0, pin="1"),
            _pad(x=10.0, y=0, pin="2"),
        ]

        strategy = orchestrator._select_strategy("NET1", intent=None, pads=pads)

        # With wide spacing and no special conditions, should use global router
        assert strategy == RoutingStrategy.GLOBAL_WITH_REPAIR

    def test_strategy_selection_fine_pitch(self, orchestrator):
        """Verify escape routing strategy for fine-pitch pads."""
        # Create fine-pitch pads (0.5mm spacing, below 0.8mm threshold)
        pads = [
            _pad(x=0, y=0, width=0.4, height=0.4, pin="1"),
            _pad(x=0.5, y=0, width=0.4, height=0.4, pin="2"),
            _pad(x=1.0, y=0, width=0.4, height=0.4, pin="3"),
        ]

        strategy = orchestrator._select_strategy("NET1", intent=None, pads=pads)

        # Fine-pitch pads should trigger escape routing
        assert strategy == RoutingStrategy.ESCAPE_THEN_GLOBAL

    def test_strategy_selection_differential_pair(self, orchestrator):
        """Verify hierarchical strategy for differential pairs."""
        # Create mock design intent for differential pair
        intent = MagicMock()
        intent.is_differential = True
        intent.impedance = 90

        pads = [
            _pad(x=0, y=0, pin="1"),
            _pad(x=10.0, y=0, pin="2"),
        ]

        strategy = orchestrator._select_strategy("USB_D+", intent=intent, pads=pads)

        # Differential pairs should use hierarchical routing
        assert strategy == RoutingStrategy.HIERARCHICAL_DIFF_PAIR

    def test_route_net_success(self, orchestrator):
        """Verify successful routing returns proper result."""
        pads = [
            _pad(x=5.0, y=5.0, pin="1"),
            _pad(x=15.0, y=5.0, pin="2"),
        ]

        result = orchestrator.route_net("NET1", pads=pads)

        assert isinstance(result, RoutingResult)
        assert result.success is True
        assert result.net == "NET1"
        assert result.strategy_used in RoutingStrategy
        assert isinstance(result.metrics, RoutingMetrics)
        assert isinstance(result.performance, PerformanceStats)

    def test_route_net_with_metrics(self, orchestrator):
        """Verify routing result includes performance metrics."""
        pads = [
            _pad(x=5.0, y=5.0, pin="1"),
            _pad(x=15.0, y=5.0, pin="2"),
        ]

        result = orchestrator.route_net("NET1", pads=pads)

        # Verify performance stats are populated
        assert result.performance.total_time_ms > 0
        assert result.performance.strategy_selection_ms >= 0
        assert result.performance.routing_ms >= 0
        assert result.performance.backend_type == "cpu"

    def test_route_global_computes_length(self, orchestrator):
        """Verify global routing computes length from corridor waypoints."""
        pads = [
            _pad(x=5.0, y=5.0, pin="1"),
            _pad(x=25.0, y=5.0, pin="2"),
        ]

        result = orchestrator._route_global("NET1", pads)

        assert result.success is True
        assert result.strategy_used == RoutingStrategy.GLOBAL_WITH_REPAIR
        # Length should be approximately the distance between pads
        assert result.metrics.total_length_mm > 0

    def test_route_global_insufficient_pads(self, orchestrator):
        """Verify global routing fails gracefully with insufficient pads."""
        pads = [_pad(x=5.0, y=5.0, pin="1")]

        result = orchestrator._route_global("NET1", pads)

        assert result.success is False
        assert "Insufficient" in result.error_message

    def test_route_escape_then_global(self, orchestrator):
        """Verify escape-then-global routing chains both phases."""
        pads = [
            _pad(x=5.0, y=5.0, width=0.4, height=0.4, pin="1"),
            _pad(x=15.0, y=5.0, width=0.4, height=0.4, pin="2"),
        ]

        result = orchestrator._route_escape_then_global("NET1", pads)

        assert result.strategy_used == RoutingStrategy.ESCAPE_THEN_GLOBAL
        # Global routing phase should still succeed even without escape grid
        assert result.success is True

    def test_route_hierarchical_with_intent(self, orchestrator):
        """Verify hierarchical routing accepts differential pair intent."""
        intent = MagicMock()
        intent.is_differential = True

        pads = [
            _pad(x=5.0, y=5.0, net=1, net_name="USB_D+", ref="J1", pin="1"),
            _pad(x=15.0, y=5.0, net=1, net_name="USB_D+", ref="U1", pin="D+"),
        ]

        result = orchestrator._route_hierarchical("USB_D+", intent, pads)

        assert result.strategy_used == RoutingStrategy.HIERARCHICAL_DIFF_PAIR
        assert isinstance(result.metrics, RoutingMetrics)

    def test_has_via_conflicts_no_grid(self, orchestrator):
        """Verify via conflict check returns False when no grid available."""
        pads = [
            _pad(x=5.0, y=5.0, pin="1"),
            _pad(x=15.0, y=5.0, pin="2"),
        ]

        # mock_pcb has no grid attribute, so should return False
        assert orchestrator._has_via_conflicts("NET1", pads) is False

    def test_has_via_conflicts_no_pads(self, orchestrator):
        """Verify via conflict check returns False with no pads."""
        assert orchestrator._has_via_conflicts("NET1", None) is False
        assert orchestrator._has_via_conflicts("NET1", []) is False

    def test_needs_escape_routing_wide_pitch(self, orchestrator):
        """Verify wide-pitch pads don't trigger escape routing."""
        pads = [
            _pad(x=0, y=0, pin="1"),
            _pad(x=2.0, y=0, pin="2"),
        ]

        needs_escape = orchestrator._needs_escape_routing(pads)
        assert needs_escape is False

    def test_needs_escape_routing_fine_pitch(self, orchestrator):
        """Verify fine-pitch pads trigger escape routing."""
        pads = [
            _pad(x=0, y=0, width=0.4, height=0.4, pin="1"),
            _pad(x=0.6, y=0, width=0.4, height=0.4, pin="2"),
        ]

        needs_escape = orchestrator._needs_escape_routing(pads)
        assert needs_escape is True

    def test_check_density_sparse(self, orchestrator):
        """Verify density calculation for sparse pads."""
        pads = [
            _pad(x=0, y=0, pin="1"),
            _pad(x=20.0, y=0, pin="2"),
        ]

        density = orchestrator._check_density(pads)
        assert 0.0 <= density < 0.5  # Sparse area

    def test_check_density_dense(self, orchestrator):
        """Verify density calculation for dense pads."""
        # Create many pads in small area (1x1mm)
        pads = [
            _pad(
                x=0.2 * i, y=0.2 * j,
                width=0.15, height=0.15,
                ref="U1", pin=f"p{i}_{j}",
            )
            for i in range(5)
            for j in range(5)
        ]

        density = orchestrator._check_density(pads)
        assert density > 0.5  # Dense area


class TestRoutingMetrics:
    """Test routing metrics data structure."""

    def test_metrics_defaults(self):
        """Verify RoutingMetrics has sensible defaults."""
        metrics = RoutingMetrics()

        assert metrics.total_length_mm == 0.0
        assert metrics.via_count == 0
        assert metrics.layer_changes == 0
        assert metrics.clearance_margin_mm == 0.0
        assert metrics.grid_points_used == 0
        assert metrics.escape_segments == 0
        assert metrics.repair_actions == 0


class TestPerformanceStats:
    """Test performance statistics data structure."""

    def test_performance_stats_defaults(self):
        """Verify PerformanceStats has sensible defaults."""
        perf = PerformanceStats()

        assert perf.total_time_ms == 0.0
        assert perf.strategy_selection_ms == 0.0
        assert perf.routing_ms == 0.0
        assert perf.repair_ms == 0.0
        assert perf.gpu_utilized is False
        assert perf.backend_type == "cpu"
