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


# ---------------------------------------------------------------------------
# Issue #1643: Net count filtering with nets_to_route_ids
# ---------------------------------------------------------------------------


class TestNetCountFiltering:
    """Numerator/denominator must use the same net population."""

    def _make_route(self, net_id):
        """Create a minimal mock route for a given net."""
        route = MagicMock()
        route.net = net_id
        route.segments = []
        route.vias = []
        return route

    def test_summary_filters_single_pad_nets(self, capsys):
        """show_routing_summary should not count single-pad nets in routed count."""
        # Routes for nets 1 (multi-pad), 2 (multi-pad), and 3 (single-pad)
        routes = [self._make_route(1), self._make_route(2), self._make_route(3)]
        router = _make_router(routes=routes)
        net_map = {"NetA": 1, "NetB": 2, "NetC": 3}

        # Only nets 1 and 2 are multi-pad signal nets
        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
            nets_to_route_ids={1, 2},
        )

        output = capsys.readouterr().out
        # Should show 2/2 (100%), not 3/2 (150%)
        assert "2/2" in output
        assert "100%" in output
        assert "3/2" not in output

    def test_summary_without_filter_counts_all(self, capsys):
        """Without nets_to_route_ids, all routed nets are counted (backward compat)."""
        routes = [self._make_route(1), self._make_route(2), self._make_route(3)]
        router = _make_router(routes=routes)
        net_map = {"NetA": 1, "NetB": 2, "NetC": 3}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
        )

        output = capsys.readouterr().out
        # Without filter, all 3 routed nets counted -> 3/2
        assert "3/2" in output

    def test_json_diagnostics_filters_routed_count(self):
        """get_routing_diagnostics_json filters nets_routed to target population."""
        routes = [self._make_route(1), self._make_route(2), self._make_route(3)]
        router = _make_router(routes=routes)
        net_map = {"NetA": 1, "NetB": 2, "NetC": 3}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=2,
            nets_to_route_ids={1, 2},
        )

        assert result["summary"]["nets_routed"] == 2
        assert result["summary"]["nets_requested"] == 2
        assert result["summary"]["success_rate"] == 100.0

    def test_json_diagnostics_without_filter(self):
        """Without filter, JSON diagnostics counts all routed nets (backward compat)."""
        routes = [self._make_route(1), self._make_route(2), self._make_route(3)]
        router = _make_router(routes=routes)
        net_map = {"NetA": 1, "NetB": 2, "NetC": 3}

        result = get_routing_diagnostics_json(
            router,
            net_map,
            nets_to_route=2,
        )

        assert result["summary"]["nets_routed"] == 3

    def test_success_rate_never_exceeds_100_percent(self, capsys):
        """With filtering, success rate should never exceed 100%."""
        # 5 single-pad nets + 2 multi-pad nets, all routed
        routes = [self._make_route(i) for i in range(1, 8)]
        router = _make_router(routes=routes)
        net_map = {f"Net{i}": i for i in range(1, 8)}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
            nets_to_route_ids={1, 2},
        )

        output = capsys.readouterr().out
        assert "2/2" in output
        assert "100%" in output

    def test_zero_nets_to_route_no_division_error(self, capsys):
        """Board with only single-pad nets should report 0/0 without error."""
        router = _make_router(routes=[])
        net_map = {"NetA": 1}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=0,
            nets_to_route_ids=set(),
        )

        output = capsys.readouterr().out
        assert "0/0" in output
        assert "0%" in output


# ---------------------------------------------------------------------------
# Issue #1833: Unrouted list must exclude skipped/single-pad nets
# ---------------------------------------------------------------------------


class TestUnroutedListFiltering:
    """The 'Unrouted nets' section must only list nets from the target
    population (nets_to_route_ids), not skipped power nets or single-pad nets.
    """

    def test_skipped_power_nets_excluded_from_unrouted(self, capsys):
        """Power nets that were intentionally skipped (not in
        nets_to_route_ids) must not appear as 'No path found'."""
        # Net 1 = GND (power, skipped), Net 2 = SIG_A (signal, routed),
        # Net 3 = SIG_B (signal, unrouted)
        route = MagicMock()
        route.net = 2
        route.net_name = "SIG_A"
        route.segments = []
        route.vias = []

        router = _make_router(routes=[route])
        net_map = {"GND": 1, "SIG_A": 2, "SIG_B": 3}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
            nets_to_route_ids={2, 3},  # only signal nets
        )

        output = capsys.readouterr().out
        # GND should NOT appear as unrouted
        assert "GND" not in output
        # SIG_B should appear as unrouted (it's a signal net that failed)
        assert "SIG_B" in output
        # Count should be 1/2
        assert "1/2" in output

    def test_single_pad_nets_excluded_from_unrouted(self, capsys):
        """Single-pad nets (not in nets_to_route_ids) must not appear
        as 'No path found'."""
        # Net 1 = LATCH (single pad), Net 2 = SIG_A (routed)
        route = MagicMock()
        route.net = 2
        route.net_name = "SIG_A"
        route.segments = []
        route.vias = []

        router = _make_router(routes=[route])
        net_map = {"LATCH": 1, "SIG_A": 2}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
            nets_to_route_ids={2},  # only multi-pad net
        )

        output = capsys.readouterr().out
        assert "LATCH" not in output
        assert "1/1" in output

    def test_unrouted_count_matches_summary(self, capsys):
        """The number of nets listed as unrouted must equal
        (denominator - numerator) from the summary line."""
        # 3 signal nets, 1 routed, 2 power nets skipped
        route = MagicMock()
        route.net = 3
        route.net_name = "SIG_A"
        route.segments = []
        route.vias = []

        router = _make_router(routes=[route])
        net_map = {"GND": 1, "+3.3V": 2, "SIG_A": 3, "SIG_B": 4, "SIG_C": 5}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=3,
            nets_to_route_ids={3, 4, 5},
        )

        output = capsys.readouterr().out
        # Should show 1/3 (only SIG_A routed out of 3 signal nets)
        assert "1/3" in output
        # GND and +3.3V must NOT appear
        assert "GND" not in output
        assert "+3.3V" not in output
        # SIG_B and SIG_C should appear as unrouted
        assert "SIG_B" in output
        assert "SIG_C" in output

    def test_legacy_no_filter_shows_all_unrouted(self, capsys):
        """When nets_to_route_ids is None (legacy), all unrouted nets
        appear (backward compat)."""
        route = MagicMock()
        route.net = 2
        route.net_name = "SIG_A"
        route.segments = []
        route.vias = []

        router = _make_router(routes=[route])
        net_map = {"GND": 1, "SIG_A": 2}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
            nets_to_route_ids=None,
        )

        output = capsys.readouterr().out
        # With no filter, GND should still appear as unrouted (legacy behavior)
        assert "GND" in output


# ---------------------------------------------------------------------------
# Issue #2498: Pour/skip nets must not be reported as failed routes
# ---------------------------------------------------------------------------


class TestIssue2498SkipNetFallback:
    """The fallback branch (when ``nets_to_route_ids`` is None) must use
    ``router.nets`` instead of ``net_map``.

    ``load_pcb_for_routing`` rewrites pads on user-skipped nets (e.g. pour
    nets like GND/VCC, named nets in route_demo.py's ``skip_nets`` list) to
    net=0, so those net IDs no longer appear in ``router.nets``.  But they
    *do* still appear in ``net_map`` for diagnostics, which previously caused
    them to be reported as "No path found" failures.
    """

    def _make_route(self, net_id):
        route = MagicMock()
        route.net = net_id
        route.segments = []
        route.vias = []
        return route

    def test_skipped_pour_nets_not_reported_when_filter_omitted(self, capsys):
        """When ``nets_to_route_ids`` is None but ``router.nets`` is a real
        dict that excludes skipped pour nets, those nets must not show up
        as 'No path found'.  Mirrors boards/03-usb-joystick where
        VBUS/VCC/GND/USB_CC1/USB_CC2 are skipped via ``skip_nets``.
        """
        # Net 2 is a multi-pad signal net that got routed.
        # Nets 1 (GND), 3 (VBUS) appeared in net_map (KiCad keeps them for
        # diagnostics) but their pads were rewritten to net=0 on load, so
        # they're absent from router.nets.
        # Net 4 is a multi-pad signal net that genuinely failed to route.
        router = _make_router(routes=[self._make_route(2)])
        # Real-shape dict: keys are net IDs, values are pad-key lists.
        router.nets = {
            2: [("U1", "1"), ("U1", "2")],  # SIG_A, routed
            4: [("U2", "1"), ("U2", "2")],  # SIG_B, unrouted
        }
        # Pads attribute set so connectivity validation block is skipped
        # (router.pads is None here).
        router.pads = None
        net_map = {
            "GND": 1,
            "SIG_A": 2,
            "VBUS": 3,
            "SIG_B": 4,
        }

        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
        )

        output = capsys.readouterr().out
        # Skipped pour nets must NOT appear as 'No path found'
        assert "GND: No path found" not in output
        assert "VBUS: No path found" not in output
        # Genuine signal failure must still appear
        assert "SIG_B" in output

    def test_single_pad_nets_excluded_from_fallback(self, capsys):
        """Even when present in ``router.nets``, single-pad nets are not
        routing candidates and must not appear as failed routes."""
        router = _make_router(routes=[self._make_route(1)])
        router.nets = {
            1: [("U1", "1"), ("U1", "2")],  # SIG_A (routed)
            2: [("TP1", "1")],  # LATCH single-pad — not routable
        }
        router.pads = None
        net_map = {"SIG_A": 1, "LATCH": 2}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=1,
        )

        output = capsys.readouterr().out
        assert "LATCH" not in output

    def test_router_nets_absent_falls_back_to_net_map(self, capsys):
        """Backward compat: when ``router.nets`` isn't a real dict (e.g. a
        MagicMock as in older tests), fall back to ``net_map`` so legacy
        callers see unchanged behavior."""
        router = _make_router(routes=[self._make_route(2)])
        # router.nets is a MagicMock attribute — not a real dict.
        net_map = {"GND": 1, "SIG_A": 2}

        show_routing_summary(
            router,
            net_map,
            nets_to_route=2,
        )

        output = capsys.readouterr().out
        # Without router.nets, we keep the legacy behavior of using net_map
        assert "GND" in output

    def test_route_demo_pattern_with_explicit_filter(self, capsys):
        """Smoke-test the route_demo.py call pattern (Option A from the
        curator analysis): pass ``nets_to_route_ids`` derived from
        ``router.nets``.  Skipped nets must not be reported and the
        unrouted-list count must match (denominator - numerator).
        """
        # Same shape as the route_demo fix: one routed signal net, one
        # unrouted signal net, and a skipped pour net that's absent from
        # router.nets but present in net_map.
        router = _make_router(routes=[self._make_route(2)])
        router.nets = {
            2: [("U1", "1"), ("U1", "2")],
            3: [("U1", "3"), ("U1", "4")],
        }
        router.pads = None
        net_map = {"GND": 1, "SIG_A": 2, "SIG_B": 3}

        # Builder-side pattern from boards/03-usb-joystick/route_demo.py
        multi_pad_net_ids = {
            net_id for net_id, pads in router.nets.items() if net_id > 0 and len(pads) >= 2
        }
        total_nets = len(multi_pad_net_ids)

        show_routing_summary(
            router,
            net_map,
            total_nets,
            nets_to_route_ids=multi_pad_net_ids,
        )

        output = capsys.readouterr().out
        assert "GND" not in output
        assert "SIG_B" in output  # genuine failure shown
        assert "1/2" in output  # 1 routed of 2 candidates
