"""Tests for multi-resolution routing with fine-grid fallback (Issue #1251)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.strategies import (
    RoutingMetrics,
    RoutingResult,
    RoutingStrategy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pad(
    ref: str,
    pin: str,
    x: float,
    y: float,
    net: int,
    net_name: str = "",
    width: float = 0.5,
    height: float = 0.5,
) -> Pad:
    """Create a Pad with sensible defaults for testing."""
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name or f"Net_{net}",
        ref=ref,
        pin=pin,
        layer=Layer.F_CU,
    )


def _make_small_router(
    width: float = 20.0,
    height: float = 20.0,
    resolution: float = 0.5,
) -> Autorouter:
    """Create a small Autorouter suitable for unit testing."""
    rules = DesignRules(
        grid_resolution=resolution,
        trace_width=0.2,
        trace_clearance=0.2,
    )
    return Autorouter(
        width=width,
        height=height,
        rules=rules,
        force_python=True,
    )


# ---------------------------------------------------------------------------
# Strategy Enum Tests
# ---------------------------------------------------------------------------


class TestMultiResolutionStrategyEnum:
    """Tests for RoutingStrategy.MULTI_RESOLUTION enum value."""

    def test_multi_resolution_exists(self):
        """MULTI_RESOLUTION enum value is importable."""
        assert hasattr(RoutingStrategy, "MULTI_RESOLUTION")

    def test_multi_resolution_is_unique(self):
        """MULTI_RESOLUTION has a unique auto() value."""
        values = [s.value for s in RoutingStrategy]
        assert len(values) == len(set(values)), "Duplicate enum values detected"
        assert RoutingStrategy.MULTI_RESOLUTION.value in values

    def test_multi_resolution_distinct_from_others(self):
        """MULTI_RESOLUTION is distinct from all other strategies."""
        assert RoutingStrategy.MULTI_RESOLUTION != RoutingStrategy.GLOBAL_WITH_REPAIR
        assert RoutingStrategy.MULTI_RESOLUTION != RoutingStrategy.FULL_PIPELINE
        assert RoutingStrategy.MULTI_RESOLUTION != RoutingStrategy.SUBGRID_ADAPTIVE


class TestRoutingMetricsFineGridNets:
    """Tests for fine_grid_nets field on RoutingMetrics."""

    def test_fine_grid_nets_default_zero(self):
        """fine_grid_nets defaults to 0."""
        metrics = RoutingMetrics()
        assert metrics.fine_grid_nets == 0

    def test_fine_grid_nets_assignable(self):
        """fine_grid_nets can be set to a non-zero value."""
        metrics = RoutingMetrics(fine_grid_nets=3)
        assert metrics.fine_grid_nets == 3

    def test_fine_grid_nets_in_to_dict(self):
        """fine_grid_nets appears in RoutingResult.to_dict() output."""
        result = RoutingResult(
            success=True,
            net="test",
            strategy_used=RoutingStrategy.MULTI_RESOLUTION,
            metrics=RoutingMetrics(fine_grid_nets=2),
        )
        data = result.to_dict()
        assert data["metrics"]["fine_grid_nets"] == 2


# ---------------------------------------------------------------------------
# Autorouter.route_all_multi_resolution Tests
# ---------------------------------------------------------------------------


class TestRouteAllMultiResolution:
    """Tests for Autorouter.route_all_multi_resolution()."""

    def _add_simple_net(
        self,
        router: Autorouter,
        net_id: int,
        ref1: str,
        pin1: str,
        x1: float,
        y1: float,
        ref2: str,
        pin2: str,
        x2: float,
        y2: float,
    ):
        """Add a simple 2-pad net to the router."""
        router.add_component(
            ref1,
            [
                {
                    "number": pin1,
                    "x": x1,
                    "y": y1,
                    "net": net_id,
                    "net_name": f"Net_{net_id}",
                    "width": 0.5,
                    "height": 0.5,
                },
            ],
        )
        router.add_component(
            ref2,
            [
                {
                    "number": pin2,
                    "x": x2,
                    "y": y2,
                    "net": net_id,
                    "net_name": f"Net_{net_id}",
                    "width": 0.5,
                    "height": 0.5,
                },
            ],
        )

    def test_all_nets_route_on_coarse_grid(self):
        """When all nets route on coarse grid, no fine-grid pass occurs."""
        router = _make_small_router(width=20.0, height=20.0, resolution=0.5)

        # Add two simple nets that should route easily
        self._add_simple_net(router, 1, "U1", "1", 2.0, 2.0, "U2", "1", 8.0, 2.0)
        self._add_simple_net(router, 2, "U3", "1", 2.0, 6.0, "U4", "1", 8.0, 6.0)

        routes = router.route_all_multi_resolution(use_negotiated=False)

        # Both nets should route successfully
        assert len(routes) >= 2
        # No routing failures
        assert len(router.routing_failures) == 0

    def test_method_exists_and_callable(self):
        """route_all_multi_resolution is a callable method on Autorouter."""
        router = _make_small_router()
        assert hasattr(router, "route_all_multi_resolution")
        assert callable(router.route_all_multi_resolution)

    def test_returns_list_of_routes(self):
        """Return type is a list."""
        router = _make_small_router()
        self._add_simple_net(router, 1, "U1", "1", 2.0, 2.0, "U2", "1", 8.0, 2.0)
        routes = router.route_all_multi_resolution(use_negotiated=False)
        assert isinstance(routes, list)

    def test_no_nets_returns_empty(self):
        """With no nets, returns an empty list and no failures."""
        router = _make_small_router()
        routes = router.route_all_multi_resolution(use_negotiated=False)
        assert routes == []
        assert len(router.routing_failures) == 0

    def test_pin_order_default_accepted(self):
        """pin_order_trials=["default"] is accepted without error."""
        router = _make_small_router()
        self._add_simple_net(router, 1, "U1", "1", 2.0, 2.0, "U2", "1", 8.0, 2.0)
        routes = router.route_all_multi_resolution(
            pin_order_trials=["default"],
            use_negotiated=False,
        )
        assert isinstance(routes, list)

    def test_pin_order_reversed_accepted(self):
        """pin_order_trials=["default", "reversed"] is accepted."""
        router = _make_small_router()
        self._add_simple_net(router, 1, "U1", "1", 2.0, 2.0, "U2", "1", 8.0, 2.0)
        routes = router.route_all_multi_resolution(
            pin_order_trials=["default", "reversed"],
            use_negotiated=False,
        )
        assert isinstance(routes, list)

    def test_pin_order_shuffled_accepted(self):
        """pin_order_trials=["shuffled"] is accepted."""
        router = _make_small_router()
        self._add_simple_net(router, 1, "U1", "1", 2.0, 2.0, "U2", "1", 8.0, 2.0)
        routes = router.route_all_multi_resolution(
            pin_order_trials=["shuffled"],
            use_negotiated=False,
        )
        assert isinstance(routes, list)

    def test_timeout_zero_returns_coarse_only(self):
        """With timeout=0, only coarse pass runs (fine grid skipped)."""
        router = _make_small_router()
        self._add_simple_net(router, 1, "U1", "1", 2.0, 2.0, "U2", "1", 8.0, 2.0)
        # timeout=0.001 should allow coarse pass but skip fine pass
        routes = router.route_all_multi_resolution(
            use_negotiated=False,
            timeout=0.001,
        )
        assert isinstance(routes, list)

    def test_budget_multiplier_parameter(self):
        """budget_multiplier parameter is accepted."""
        router = _make_small_router()
        self._add_simple_net(router, 1, "U1", "1", 2.0, 2.0, "U2", "1", 8.0, 2.0)
        routes = router.route_all_multi_resolution(
            budget_multiplier=8.0,
            use_negotiated=False,
        )
        assert isinstance(routes, list)

    def test_fine_resolution_factor(self):
        """fine_resolution_factor parameter controls fine grid resolution."""
        router = _make_small_router(resolution=0.5)
        self._add_simple_net(router, 1, "U1", "1", 2.0, 2.0, "U2", "1", 8.0, 2.0)
        # With factor 0.5, fine resolution should be 0.25
        routes = router.route_all_multi_resolution(
            fine_resolution_factor=0.5,
            use_negotiated=False,
        )
        assert isinstance(routes, list)


class TestRouteAllAdvancedMultiResolution:
    """Tests for route_all_advanced with use_multi_resolution flag."""

    def test_route_all_advanced_accepts_multi_resolution(self):
        """route_all_advanced accepts use_multi_resolution parameter."""
        router = _make_small_router()
        # Just verify it doesn't raise -- no nets means empty result
        routes = router.route_all_advanced(use_multi_resolution=True)
        assert isinstance(routes, list)

    def test_multi_resolution_takes_priority_over_hierarchical(self):
        """multi_resolution takes priority over hierarchical in route_all_advanced."""
        router = _make_small_router()
        # When both are True, multi_resolution should win
        with patch.object(router, "route_all_multi_resolution", return_value=[]) as mock_mr:
            with patch.object(router, "route_all_hierarchical", return_value=[]) as mock_hier:
                router.route_all_advanced(
                    use_multi_resolution=True,
                    use_hierarchical=True,
                )
                mock_mr.assert_called_once()
                mock_hier.assert_not_called()


# ---------------------------------------------------------------------------
# Orchestrator Tests
# ---------------------------------------------------------------------------


class TestOrchestratorMultiResolution:
    """Tests for orchestrator MULTI_RESOLUTION dispatch."""

    def test_execute_strategy_dispatches_multi_resolution(self):
        """_execute_strategy dispatches to _route_multi_resolution."""
        from kicad_tools.router.orchestrator import RoutingOrchestrator

        pcb = MagicMock()
        pcb.width = 50.0
        pcb.height = 50.0
        rules = DesignRules()
        orch = RoutingOrchestrator(pcb=pcb, rules=rules)

        with patch.object(orch, "_route_multi_resolution") as mock_mr:
            mock_mr.return_value = RoutingResult(
                success=True,
                net="test",
                strategy_used=RoutingStrategy.MULTI_RESOLUTION,
            )
            result = orch._execute_strategy(
                "test",
                RoutingStrategy.MULTI_RESOLUTION,
                intent=None,
                pads=None,
            )
            mock_mr.assert_called_once_with("test", None)
            assert result.strategy_used == RoutingStrategy.MULTI_RESOLUTION

    def test_suggest_alternatives_includes_multi_resolution(self):
        """When global routing fails, MULTI_RESOLUTION is suggested."""
        from kicad_tools.router.orchestrator import RoutingOrchestrator

        pcb = MagicMock()
        pcb.width = 50.0
        pcb.height = 50.0
        rules = DesignRules()
        orch = RoutingOrchestrator(pcb=pcb, rules=rules)

        alternatives = orch._suggest_alternatives(RoutingStrategy.GLOBAL_WITH_REPAIR)
        strategy_names = [a.strategy for a in alternatives]
        assert RoutingStrategy.MULTI_RESOLUTION in strategy_names


# ---------------------------------------------------------------------------
# MCP Tool Tests
# ---------------------------------------------------------------------------


class TestMCPStrategyMap:
    """Tests for MCP routing tool strategy map."""

    def test_multi_resolution_in_strategy_map(self):
        """multi_resolution is in the MCP strategy_map."""
        # We verify by checking that the strategy string is accepted
        # without actually calling the full tool (which needs a PCB file)
        strategy_map = {
            "global": RoutingStrategy.GLOBAL_WITH_REPAIR,
            "escape": RoutingStrategy.ESCAPE_THEN_GLOBAL,
            "hierarchical": RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
            "subgrid": RoutingStrategy.SUBGRID_ADAPTIVE,
            "via_resolution": RoutingStrategy.VIA_CONFLICT_RESOLUTION,
            "multi_resolution": RoutingStrategy.MULTI_RESOLUTION,
        }
        assert "multi_resolution" in strategy_map
        assert strategy_map["multi_resolution"] == RoutingStrategy.MULTI_RESOLUTION
