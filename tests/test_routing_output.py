"""Tests for routing output and diagnostics display.

Verifies:
- Bug #1267a: Empty "Detailed Failure Analysis" section is not printed when
  no RoutingFailure objects have detailed analysis.
- Bug #1267b: Strategy suggestions are filtered to exclude the current strategy.
- JSON diagnostics also filter strategy suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from unittest.mock import MagicMock

from kicad_tools.router.output import (
    get_routing_diagnostics_json,
    show_routing_summary,
)

# ---------------------------------------------------------------------------
# Lightweight stubs so we don't need to instantiate real Autorouter / Grid
# ---------------------------------------------------------------------------


class _FailureCause(Enum):
    UNKNOWN = "unknown"
    ROUTING_ORDER = "routing_order"
    CONGESTION = "congestion"


@dataclass
class _RoutingFailure:
    net: int
    net_name: str
    source_pad: tuple[str, str]
    target_pad: tuple[str, str]
    source_coords: tuple[float, float] | None = None
    target_coords: tuple[float, float] | None = None
    blocking_nets: set[int] = field(default_factory=set)
    blocking_components: list[str] = field(default_factory=list)
    reason: str = "No path found"
    failure_cause: _FailureCause = _FailureCause.UNKNOWN
    analysis: object | None = None


def _make_router(
    routes=None,
    routing_failures=None,
    grid_num_layers=2,
    grid_resolution=0.25,
):
    """Build a minimal mock that satisfies show_routing_summary / get_routing_diagnostics_json."""
    router = MagicMock()
    router.routes = routes or []
    router.routing_failures = routing_failures or []
    router.grid.num_layers = grid_num_layers
    router.grid.resolution = grid_resolution
    return router


# ---------------------------------------------------------------------------
# Bug 1: Empty "Detailed Failure Analysis" section
# ---------------------------------------------------------------------------


class TestDetailedFailureAnalysisSection:
    """The 'Detailed Failure Analysis' header must not appear when there are
    no RoutingFailure records with content."""

    def test_no_header_when_no_failures(self, capsys):
        """verbose=True with unrouted nets but no failure records should NOT
        print the 'Detailed Failure Analysis' header."""
        router = _make_router()
        net_map = {"NetA": 1, "NetB": 2}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
            verbose=True,
        )

        output = capsys.readouterr().out
        assert "Detailed Failure Analysis" not in output

    def test_no_header_when_failures_lack_detail(self, capsys):
        """Even with RoutingFailure objects, if they belong to *routed* nets
        (not in unrouted_ids), the section should not appear."""
        # Net 1 is routed (has a route), net 2 is unrouted but has no failures
        route = MagicMock()
        route.net = 1
        route.segments = []
        route.vias = []
        router = _make_router(routes=[route])
        net_map = {"NetA": 1, "NetB": 2}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
            verbose=True,
        )

        output = capsys.readouterr().out
        # Net 2 is unrouted but there are no failure records for it
        assert "Detailed Failure Analysis" not in output

    def test_header_appears_when_failures_exist(self, capsys):
        """When there are unrouted nets with failure records, the header
        SHOULD appear."""
        failure = _RoutingFailure(
            net=1,
            net_name="NetA",
            source_pad=("R1", "1"),
            target_pad=("R2", "1"),
            failure_cause=_FailureCause.UNKNOWN,
        )
        router = _make_router(routing_failures=[failure])
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            verbose=True,
        )

        output = capsys.readouterr().out
        assert "Detailed Failure Analysis" in output

    def test_no_header_when_verbose_false(self, capsys):
        """The section is verbose-only, so verbose=False should never show it."""
        failure = _RoutingFailure(
            net=1,
            net_name="NetA",
            source_pad=("R1", "1"),
            target_pad=("R2", "1"),
            failure_cause=_FailureCause.UNKNOWN,
        )
        router = _make_router(routing_failures=[failure])
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            verbose=False,
        )

        output = capsys.readouterr().out
        assert "Detailed Failure Analysis" not in output


# ---------------------------------------------------------------------------
# Bug 2: Strategy-aware suggestions in show_routing_summary
# ---------------------------------------------------------------------------


class TestStrategyAwareSuggestions:
    """Routing suggestions must exclude the strategy that was just used."""

    def test_basic_strategy_shows_both_alternatives(self, capsys):
        """With current_strategy='basic', both negotiated and monte-carlo
        should be suggested."""
        router = _make_router()
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="basic",
        )

        output = capsys.readouterr().out
        assert "--strategy negotiated" in output
        assert "--strategy monte-carlo" in output

    def test_negotiated_strategy_excludes_negotiated(self, capsys):
        """With current_strategy='negotiated', the suggestion must NOT
        recommend --strategy negotiated."""
        router = _make_router()
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="negotiated",
        )

        output = capsys.readouterr().out
        assert "--strategy negotiated" not in output
        assert "--strategy monte-carlo" in output

    def test_monte_carlo_strategy_excludes_monte_carlo(self, capsys):
        """With current_strategy='monte-carlo', the suggestion must NOT
        recommend --strategy monte-carlo."""
        router = _make_router()
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="monte-carlo",
        )

        output = capsys.readouterr().out
        assert "--strategy monte-carlo" not in output
        assert "--strategy negotiated" in output

    def test_routing_order_suggestion_excludes_negotiated(self, capsys):
        """When routing_order failures exist and current_strategy is 'negotiated',
        the ROUTING ORDER section should NOT suggest negotiated."""
        failure = _RoutingFailure(
            net=1,
            net_name="NetA",
            source_pad=("R1", "1"),
            target_pad=("R2", "1"),
            failure_cause=_FailureCause.ROUTING_ORDER,
            reason="Blocked by earlier route",
        )
        router = _make_router(routing_failures=[failure])
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="negotiated",
        )

        output = capsys.readouterr().out
        assert "ROUTING ORDER" in output
        assert "--strategy negotiated" not in output

    def test_routing_order_suggestion_suggests_negotiated_when_basic(self, capsys):
        """When routing_order failures exist and current_strategy is 'basic',
        the suggestion should recommend negotiated."""
        failure = _RoutingFailure(
            net=1,
            net_name="NetA",
            source_pad=("R1", "1"),
            target_pad=("R2", "1"),
            failure_cause=_FailureCause.ROUTING_ORDER,
            reason="Blocked by earlier route",
        )
        router = _make_router(routing_failures=[failure])
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="basic",
        )

        output = capsys.readouterr().out
        assert "ROUTING ORDER" in output
        assert "--strategy negotiated" in output

    def test_default_strategy_is_basic(self, capsys):
        """When current_strategy is not specified, it defaults to 'basic'
        and both negotiated and monte-carlo are suggested."""
        router = _make_router()
        net_map = {"NetA": 1}

        # Don't pass current_strategy -- should default to "basic"
        show_routing_summary(router, net_map, nets_to_route=1)

        output = capsys.readouterr().out
        assert "--strategy negotiated" in output
        assert "--strategy monte-carlo" in output


# ---------------------------------------------------------------------------
# Strategy-aware suggestions in JSON output
# ---------------------------------------------------------------------------


class TestJsonStrategyAwareSuggestions:
    """get_routing_diagnostics_json must also filter strategies."""

    def test_json_basic_shows_both(self):
        router = _make_router()
        net_map = {"NetA": 1}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="basic",
        )

        suggestion_fixes = [s.get("fix", "") for s in result["suggestions"]]
        fix_text = " ".join(suggestion_fixes)
        assert "negotiated" in fix_text
        assert "monte-carlo" in fix_text

    def test_json_negotiated_excludes_negotiated(self):
        router = _make_router()
        net_map = {"NetA": 1}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="negotiated",
        )

        suggestion_fixes = [s.get("fix", "") for s in result["suggestions"]]
        fix_text = " ".join(suggestion_fixes)
        assert "--strategy negotiated" not in fix_text
        assert "monte-carlo" in fix_text

    def test_json_monte_carlo_excludes_monte_carlo(self):
        router = _make_router()
        net_map = {"NetA": 1}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="monte-carlo",
        )

        suggestion_fixes = [s.get("fix", "") for s in result["suggestions"]]
        fix_text = " ".join(suggestion_fixes)
        assert "--strategy monte-carlo" not in fix_text
        assert "negotiated" in fix_text

    def test_json_routing_order_with_negotiated_strategy(self):
        """Routing order failures in JSON should suggest an alternative
        to the current strategy."""
        failure = _RoutingFailure(
            net=1,
            net_name="NetA",
            source_pad=("R1", "1"),
            target_pad=("R2", "1"),
            failure_cause=_FailureCause.ROUTING_ORDER,
            reason="Blocked by earlier route",
        )
        router = _make_router(routing_failures=[failure])
        net_map = {"NetA": 1}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=1,
            current_strategy="negotiated",
        )

        routing_order_suggestions = [
            s for s in result["suggestions"] if s.get("category") == "ROUTING_ORDER"
        ]
        assert len(routing_order_suggestions) == 1
        assert "--strategy negotiated" not in routing_order_suggestions[0]["fix"]

    def test_json_default_strategy_is_basic(self):
        """When current_strategy is not specified, it defaults to 'basic'."""
        router = _make_router()
        net_map = {"NetA": 1}

        result = get_routing_diagnostics_json(router, net_map, nets_to_route=1)

        suggestion_fixes = [s.get("fix", "") for s in result["suggestions"]]
        fix_text = " ".join(suggestion_fixes)
        assert "negotiated" in fix_text
        assert "monte-carlo" in fix_text
