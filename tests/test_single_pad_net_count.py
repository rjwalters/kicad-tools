"""Tests for single-pad net exclusion from routing counts (#2414).

Verifies:
1. AdaptiveAutorouter._check_convergence() excludes single-pad nets
2. AdaptiveAutorouter.route() sets nets_requested excluding single-pad nets
3. RoutingResult.single_pad_count is populated correctly
4. show_routing_summary() reports single-pad exclusion in text output
5. get_routing_diagnostics_json() includes single_pad_nets in JSON summary
6. RoutingResult.success_rate returns 1.0 when only single-pad nets exist
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kicad_tools.router.adaptive import RoutingResult
from kicad_tools.router.output import (
    get_routing_diagnostics_json,
    show_routing_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_router_mock(
    nets: dict[int, list] | None = None,
    routes: list | None = None,
    routing_failures: list | None = None,
    grid_num_layers: int = 2,
):
    """Build a minimal mock that satisfies show_routing_summary / get_routing_diagnostics_json."""
    router = MagicMock()
    router.nets = nets or {}
    router.routes = routes or []
    router.routing_failures = routing_failures or []
    router.grid.num_layers = grid_num_layers
    router.grid.resolution = 0.25
    # pads attribute needed for connectivity validation path
    router.pads = {}
    return router


# ---------------------------------------------------------------------------
# RoutingResult unit tests
# ---------------------------------------------------------------------------


class TestRoutingResultSinglePadCount:
    """RoutingResult should correctly handle single_pad_count field."""

    def test_single_pad_count_default_zero(self):
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=MagicMock(),
            nets_requested=5,
            nets_routed=5,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
        )
        assert result.single_pad_count == 0

    def test_single_pad_count_populated(self):
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=MagicMock(),
            nets_requested=5,
            nets_routed=5,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
            single_pad_count=3,
        )
        assert result.single_pad_count == 3

    def test_success_rate_all_single_pad(self):
        """When nets_requested is 0 (all nets are single-pad), success_rate
        should return 1.0 (100%) not raise ZeroDivisionError."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=MagicMock(),
            nets_requested=0,
            nets_routed=0,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
            single_pad_count=5,
        )
        assert result.success_rate == 1.0


# ---------------------------------------------------------------------------
# AdaptiveAutorouter._check_convergence tests
# ---------------------------------------------------------------------------


class TestCheckConvergenceSinglePad:
    """_check_convergence must exclude single-pad nets from nets_requested."""

    def test_convergence_with_single_pad_nets(self):
        """When all multi-pad nets are routed, convergence should be True
        even if single-pad nets exist."""
        from kicad_tools.router.adaptive import AdaptiveAutorouter

        adaptive = AdaptiveAutorouter.__new__(AdaptiveAutorouter)

        # Build a mock router with:
        # - Net 1: single-pad (should be excluded)
        # - Net 2: multi-pad, routed
        # - Net 0: unconnected (always excluded)
        router = MagicMock()
        router.nets = {
            0: [("U1", "GND")],
            1: [("U1", "1")],           # single-pad
            2: [("U1", "2"), ("R1", "1")],  # multi-pad
        }
        route = MagicMock()
        route.net = 2
        router.routes = [route]

        result = adaptive._check_convergence(router, overflow=0)
        assert result is True, (
            "Convergence should be True when all multi-pad nets are routed"
        )

    def test_no_convergence_when_multi_pad_unrouted(self):
        """When a multi-pad net is unrouted, convergence should be False."""
        from kicad_tools.router.adaptive import AdaptiveAutorouter

        adaptive = AdaptiveAutorouter.__new__(AdaptiveAutorouter)

        router = MagicMock()
        router.nets = {
            0: [("U1", "GND")],
            1: [("U1", "1")],                  # single-pad
            2: [("U1", "2"), ("R1", "1")],      # multi-pad, routed
            3: [("U1", "3"), ("R2", "1")],      # multi-pad, NOT routed
        }
        route = MagicMock()
        route.net = 2
        router.routes = [route]

        result = adaptive._check_convergence(router, overflow=0)
        assert result is False

    def test_convergence_all_single_pad(self):
        """Board with only single-pad nets should converge immediately."""
        from kicad_tools.router.adaptive import AdaptiveAutorouter

        adaptive = AdaptiveAutorouter.__new__(AdaptiveAutorouter)

        router = MagicMock()
        router.nets = {
            0: [("U1", "GND")],
            1: [("U1", "1")],
            2: [("U2", "1")],
        }
        router.routes = []

        result = adaptive._check_convergence(router, overflow=0)
        assert result is True


# ---------------------------------------------------------------------------
# show_routing_summary single-pad reporting tests
# ---------------------------------------------------------------------------


class TestShowRoutingSummarySinglePad:
    """show_routing_summary should report single-pad net exclusion."""

    def test_single_pad_count_in_text_output(self, capsys):
        """When single_pad_count > 0, the text summary should mention it."""
        route = MagicMock()
        route.net = 1
        route.segments = []
        route.vias = []
        router = _make_router_mock(routes=[route])
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            single_pad_count=3,
        )

        output = capsys.readouterr().out
        assert "3 single-pad net(s) excluded" in output

    def test_no_single_pad_line_when_zero(self, capsys):
        """When single_pad_count is 0, no single-pad text should appear."""
        route = MagicMock()
        route.net = 1
        route.segments = []
        route.vias = []
        router = _make_router_mock(routes=[route])
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            single_pad_count=0,
        )

        output = capsys.readouterr().out
        assert "single-pad" not in output


# ---------------------------------------------------------------------------
# get_routing_diagnostics_json single-pad reporting tests
# ---------------------------------------------------------------------------


class TestDiagnosticsJsonSinglePad:
    """get_routing_diagnostics_json should include single_pad_nets in summary."""

    def test_single_pad_nets_in_json(self):
        """JSON summary should include single_pad_nets and total_nets_on_board."""
        route = MagicMock()
        route.net = 1
        route.segments = []
        route.vias = []
        router = _make_router_mock(routes=[route])
        net_map = {"NetA": 1}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=1,
            single_pad_count=5,
        )

        assert result["summary"]["single_pad_nets"] == 5
        assert result["summary"]["total_nets_on_board"] == 6  # 1 + 5

    def test_no_single_pad_keys_when_zero(self):
        """When single_pad_count is 0, the keys should not be present."""
        route = MagicMock()
        route.net = 1
        route.segments = []
        route.vias = []
        router = _make_router_mock(routes=[route])
        net_map = {"NetA": 1}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=1,
            single_pad_count=0,
        )

        assert "single_pad_nets" not in result["summary"]
        assert "total_nets_on_board" not in result["summary"]

    def test_json_success_rate_excludes_single_pad(self):
        """success_rate in JSON should be based on multi-pad nets only."""
        route = MagicMock()
        route.net = 1
        route.segments = []
        route.vias = []
        router = _make_router_mock(routes=[route])
        net_map = {"NetA": 1}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=1,  # only multi-pad nets
            single_pad_count=10,
        )

        # 1 routed / 1 requested = 100%
        assert result["summary"]["success_rate"] == 100.0
        assert result["summary"]["nets_requested"] == 1
