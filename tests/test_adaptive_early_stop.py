"""Tests for adaptive-rules early termination (issue #2380).

Verifies that the adaptive-rules tier loop skips remaining tiers when
completion regresses, and that --no-early-stop disables this behavior.
"""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class FakeTier:
    """Minimal relaxation tier for testing."""

    tier: int
    description: str
    trace_width: float
    clearance: float
    via_drill: float = 0.3
    via_diameter: float = 0.6


def _make_fake_router(nets_routed: int, nets_total: int = 20,
                      segments: int = 0, vias: int = 0):
    """Create a mock router that reports the given routing stats."""
    router = MagicMock()
    router.nets = {i: [1, 2] for i in range(1, nets_total + 1)}
    router.grid = MagicMock(width=50, height=50)
    router.routes = []
    router._pour_nets_without_zones = set()
    router.get_statistics.return_value = {
        "nets_routed": nets_routed,
        "segments": segments,
        "vias": vias,
    }
    return router


def _make_args(**overrides):
    """Create a minimal args namespace for route_with_rule_relaxation."""
    defaults = dict(
        grid=0.1,
        trace_width=0.2,
        clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        manufacturer="jlcpcb",
        min_trace=None,
        min_clearance_floor=None,
        strategy="negotiated",
        iterations=5,
        timeout=60,
        skip_nets=None,
        edge_clearance=None,
        force=False,
        backend="python",
        verbose=False,
        min_completion=0.95,
        no_optimize=True,
        no_early_stop=False,
        multi_resolution=False,
        two_phase=False,
        per_net_timeout=None,
        two_phase_iterations=None,
        batch_routing=False,
        high_performance=False,
        hierarchical=False,
        perturbation=True,
        mc_trials=10,
        escape_routing=None,
        skip_drc=True,
        diagnostics=False,
        layers="auto",
        dry_run=True,
        pcb="test.kicad_pcb",
        max_layers=6,
        auto_fix=False,
        auto_fix_passes=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patches_for_relaxation(tiers, fake_load_pcb):
    """Return a list of patch context managers for route_with_rule_relaxation."""
    from contextlib import ExitStack

    from kicad_tools.router import LayerStack

    stack = ExitStack()
    # Patch at the source module since route_with_rule_relaxation uses lazy imports
    stack.enter_context(
        patch("kicad_tools.router.get_relaxation_tiers", return_value=tiers)
    )
    stack.enter_context(
        patch("kicad_tools.router.load_pcb_for_routing", side_effect=fake_load_pcb)
    )
    stack.enter_context(
        patch("kicad_tools.router.is_cpp_available", return_value=False)
    )
    stack.enter_context(
        patch(
            "kicad_tools.cli.route_cmd._auto_skip_pour_nets",
            return_value=([], []),
        )
    )
    stack.enter_context(
        patch(
            "kicad_tools.cli.route_cmd._resolve_escape_routing_flag",
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "kicad_tools.cli.route_cmd._should_use_escape_routing",
            return_value=False,
        )
    )
    stack.enter_context(
        patch(
            "kicad_tools.router.io.detect_layer_stack",
            return_value=LayerStack.two_layer(),
        )
    )
    stack.enter_context(
        patch("kicad_tools.router.show_routing_summary")
    )
    stack.enter_context(
        patch(
            "kicad_tools.router.get_mfr_limits",
            return_value=MagicMock(min_trace=0.127, min_clearance=0.127),
        )
    )
    return stack


class TestAdaptiveRulesEarlyStop:
    """Tests for early termination when completion regresses."""

    def test_skips_remaining_tiers_on_regression(self):
        """When tier 2 is worse than tier 1, tier 3 is skipped."""
        from kicad_tools.cli.route_cmd import route_with_rule_relaxation

        tiers = [
            FakeTier(0, "user", 0.2, 0.15),
            FakeTier(1, "moderate", 0.15, 0.127),
            FakeTier(2, "aggressive", 0.127, 0.1),
        ]

        # Tier 0 routes 15/20, tier 1 routes 12/20 (regression), tier 2 never runs
        routers = [
            _make_fake_router(15, 20),
            _make_fake_router(12, 20),
            _make_fake_router(13, 20),  # should not be created
        ]
        router_idx = [0]

        def fake_load_pcb(*args, **kwargs):
            r = routers[router_idx[0]]
            router_idx[0] += 1
            return r, {"net1": 1}

        args = _make_args()

        with _patches_for_relaxation(tiers, fake_load_pcb):
            route_with_rule_relaxation(
                pcb_path=MagicMock(),
                output_path=MagicMock(),
                args=args,
                quiet=True,
            )

        # Only 2 routers should have been used (tier 2 skipped)
        assert router_idx[0] == 2

    def test_all_tiers_run_when_improving(self):
        """When each tier improves, all tiers run."""
        from kicad_tools.cli.route_cmd import route_with_rule_relaxation

        tiers = [
            FakeTier(0, "user", 0.2, 0.15),
            FakeTier(1, "moderate", 0.15, 0.127),
            FakeTier(2, "aggressive", 0.127, 0.1),
        ]

        # Tier 0: 15/20, tier 1: 17/20, tier 2: 20/20 (success)
        routers = [
            _make_fake_router(15, 20),
            _make_fake_router(17, 20),
            _make_fake_router(20, 20),
        ]
        router_idx = [0]

        def fake_load_pcb(*args, **kwargs):
            r = routers[router_idx[0]]
            router_idx[0] += 1
            return r, {"net1": 1}

        args = _make_args()

        with _patches_for_relaxation(tiers, fake_load_pcb):
            route_with_rule_relaxation(
                pcb_path=MagicMock(),
                output_path=MagicMock(),
                args=args,
                quiet=True,
            )

        # All 3 tiers ran (tier 2 hit success threshold)
        assert router_idx[0] == 3

    def test_no_early_stop_disables_heuristic(self):
        """With --no-early-stop, all tiers run even on regression."""
        from kicad_tools.cli.route_cmd import route_with_rule_relaxation

        tiers = [
            FakeTier(0, "user", 0.2, 0.15),
            FakeTier(1, "moderate", 0.15, 0.127),
            FakeTier(2, "aggressive", 0.127, 0.1),
        ]

        # Tier 0: 15/20, tier 1: 12/20 (regression), tier 2: 13/20
        routers = [
            _make_fake_router(15, 20),
            _make_fake_router(12, 20),
            _make_fake_router(13, 20),
        ]
        router_idx = [0]

        def fake_load_pcb(*args, **kwargs):
            r = routers[router_idx[0]]
            router_idx[0] += 1
            return r, {"net1": 1}

        args = _make_args(no_early_stop=True)

        with _patches_for_relaxation(tiers, fake_load_pcb):
            route_with_rule_relaxation(
                pcb_path=MagicMock(),
                output_path=MagicMock(),
                args=args,
                quiet=True,
            )

        # All 3 tiers ran because --no-early-stop was set
        assert router_idx[0] == 3

    def test_single_tier_no_regression_check(self):
        """With only one tier, no regression check is needed."""
        from kicad_tools.cli.route_cmd import route_with_rule_relaxation

        tiers = [
            FakeTier(0, "user", 0.2, 0.15),
        ]

        routers = [_make_fake_router(15, 20)]
        router_idx = [0]

        def fake_load_pcb(*args, **kwargs):
            r = routers[router_idx[0]]
            router_idx[0] += 1
            return r, {"net1": 1}

        args = _make_args()

        with _patches_for_relaxation(tiers, fake_load_pcb):
            route_with_rule_relaxation(
                pcb_path=MagicMock(),
                output_path=MagicMock(),
                args=args,
                quiet=True,
            )

        # The single tier ran
        assert router_idx[0] == 1

    def test_returns_best_result_on_early_stop(self):
        """When early-stopping, the best result (tier 0) is used as final."""
        from kicad_tools.cli.route_cmd import route_with_rule_relaxation

        tiers = [
            FakeTier(0, "user", 0.2, 0.15),
            FakeTier(1, "moderate", 0.15, 0.127),
            FakeTier(2, "aggressive", 0.127, 0.1),
        ]

        # Tier 0: 15/20, tier 1: 12/20 (regression triggers early stop)
        routers = [
            _make_fake_router(15, 20),
            _make_fake_router(12, 20),
        ]
        router_idx = [0]

        def fake_load_pcb(*args, **kwargs):
            r = routers[router_idx[0]]
            router_idx[0] += 1
            return r, {"net1": 1}

        # min_completion=0.9 so neither tier is "success" (15/20=75%, 12/20=60%)
        args = _make_args(min_completion=0.9)

        with _patches_for_relaxation(tiers, fake_load_pcb):
            result = route_with_rule_relaxation(
                pcb_path=MagicMock(),
                output_path=MagicMock(),
                args=args,
                quiet=True,
            )

        # Function returns 0 since best_result exists (saves to output)
        assert result == 0
        # Only 2 tiers ran (tier 2 was skipped)
        assert router_idx[0] == 2


class TestCombinedEscalationEarlyStop:
    """Tests for early termination in combined escalation (2D search)."""

    def test_skips_remaining_tiers_per_layer(self):
        """In 2D search, regression in tiers skips remaining tiers for that layer."""
        from kicad_tools.cli.route_cmd import route_with_combined_escalation

        tiers = [
            FakeTier(0, "user", 0.2, 0.15),
            FakeTier(1, "moderate", 0.15, 0.127),
        ]

        # 2L tier 0: 15/20, 2L tier 1: 12/20 (regression, skip tier 1 for 2L)
        routers = [
            _make_fake_router(15, 20),  # 2L, tier 0
            _make_fake_router(12, 20),  # 2L, tier 1 (regression)
        ]
        router_idx = [0]

        def fake_load_pcb(*args, **kwargs):
            r = routers[router_idx[0]]
            router_idx[0] += 1
            return r, {"net1": 1}

        args = _make_args(max_layers=2)

        with _patches_for_relaxation(tiers, fake_load_pcb):
            route_with_combined_escalation(
                pcb_path=MagicMock(),
                output_path=MagicMock(),
                args=args,
                quiet=True,
            )

        # Only 2 attempts: 2L tier 0 + 2L tier 1 (regression detected)
        assert router_idx[0] == 2


class TestIsBetterResult:
    """Unit tests for _is_better_result tiebreaker helper (Issue #2397)."""

    def test_higher_completion_wins(self):
        """When completions differ, higher completion wins regardless of segments."""
        from kicad_tools.cli.route_cmd import (
            RuleRelaxationResult,
            _is_better_result,
        )

        better = RuleRelaxationResult(
            tier=0, trace_width=0.2, clearance=0.15, via_drill=0.3,
            via_diameter=0.6, tier_description="user", router=None,
            net_map={}, nets_routed=10, nets_to_route=20,
            completion=0.5, success=False, layer_count=2,
            stats={"segments": 5, "vias": 0},
        )
        worse = RuleRelaxationResult(
            tier=1, trace_width=0.15, clearance=0.127, via_drill=0.3,
            via_diameter=0.6, tier_description="moderate", router=None,
            net_map={}, nets_routed=5, nets_to_route=20,
            completion=0.25, success=False, layer_count=2,
            stats={"segments": 100, "vias": 20},
        )

        assert _is_better_result(better, worse) is True
        assert _is_better_result(worse, better) is False

    def test_tied_completion_more_segments_wins(self):
        """When completion ties, result with more segments wins."""
        from kicad_tools.cli.route_cmd import (
            RuleRelaxationResult,
            _is_better_result,
        )

        more_segments = RuleRelaxationResult(
            tier=1, trace_width=0.15, clearance=0.127, via_drill=0.3,
            via_diameter=0.6, tier_description="moderate", router=None,
            net_map={}, nets_routed=0, nets_to_route=20,
            completion=0.0, success=False, layer_count=2,
            stats={"segments": 50, "vias": 3},
        )
        fewer_segments = RuleRelaxationResult(
            tier=0, trace_width=0.2, clearance=0.15, via_drill=0.3,
            via_diameter=0.6, tier_description="user", router=None,
            net_map={}, nets_routed=0, nets_to_route=20,
            completion=0.0, success=False, layer_count=2,
            stats={"segments": 10, "vias": 1},
        )

        assert _is_better_result(more_segments, fewer_segments) is True
        assert _is_better_result(fewer_segments, more_segments) is False

    def test_tied_completion_and_segments_more_vias_wins(self):
        """When completion and segments tie, result with more vias wins."""
        from kicad_tools.cli.route_cmd import (
            RuleRelaxationResult,
            _is_better_result,
        )

        more_vias = RuleRelaxationResult(
            tier=1, trace_width=0.15, clearance=0.127, via_drill=0.3,
            via_diameter=0.6, tier_description="moderate", router=None,
            net_map={}, nets_routed=0, nets_to_route=20,
            completion=0.0, success=False, layer_count=2,
            stats={"segments": 50, "vias": 10},
        )
        fewer_vias = RuleRelaxationResult(
            tier=0, trace_width=0.2, clearance=0.15, via_drill=0.3,
            via_diameter=0.6, tier_description="user", router=None,
            net_map={}, nets_routed=0, nets_to_route=20,
            completion=0.0, success=False, layer_count=2,
            stats={"segments": 50, "vias": 2},
        )

        assert _is_better_result(more_vias, fewer_vias) is True
        assert _is_better_result(fewer_vias, more_vias) is False

    def test_full_cascade_fewer_layers_wins(self):
        """When completion, segments, and vias all tie, fewer layers wins."""
        from kicad_tools.cli.route_cmd import (
            RuleRelaxationResult,
            _is_better_result,
        )

        fewer_layers = RuleRelaxationResult(
            tier=0, trace_width=0.2, clearance=0.15, via_drill=0.3,
            via_diameter=0.6, tier_description="user", router=None,
            net_map={}, nets_routed=0, nets_to_route=20,
            completion=0.0, success=False, layer_count=2,
            stats={"segments": 50, "vias": 5},
        )
        more_layers = RuleRelaxationResult(
            tier=0, trace_width=0.2, clearance=0.15, via_drill=0.3,
            via_diameter=0.6, tier_description="user", router=None,
            net_map={}, nets_routed=0, nets_to_route=20,
            completion=0.0, success=False, layer_count=4,
            stats={"segments": 50, "vias": 5},
        )

        assert _is_better_result(fewer_layers, more_layers) is True
        assert _is_better_result(more_layers, fewer_layers) is False

    def test_none_stats_handled_gracefully(self):
        """Results with None stats use 0 defaults for tiebreaker fields."""
        from kicad_tools.cli.route_cmd import (
            RuleRelaxationResult,
            _is_better_result,
        )

        with_stats = RuleRelaxationResult(
            tier=0, trace_width=0.2, clearance=0.15, via_drill=0.3,
            via_diameter=0.6, tier_description="user", router=None,
            net_map={}, nets_routed=0, nets_to_route=20,
            completion=0.0, success=False, layer_count=2,
            stats={"segments": 10, "vias": 1},
        )
        no_stats = RuleRelaxationResult(
            tier=0, trace_width=0.2, clearance=0.15, via_drill=0.3,
            via_diameter=0.6, tier_description="user", router=None,
            net_map={}, nets_routed=0, nets_to_route=20,
            completion=0.0, success=False, layer_count=2,
            stats=None,
        )

        assert _is_better_result(with_stats, no_stats) is True
        assert _is_better_result(no_stats, with_stats) is False

    def test_layer_escalation_result_works(self):
        """_is_better_result works with LayerEscalationResult too."""
        from kicad_tools.cli.route_cmd import (
            LayerEscalationResult,
            _is_better_result,
        )

        better = LayerEscalationResult(
            layer_count=2, layer_stack=None, router=None, net_map={},
            nets_routed=0, nets_to_route=20, completion=0.0, success=False,
            stats={"segments": 30, "vias": 5},
        )
        worse = LayerEscalationResult(
            layer_count=4, layer_stack=None, router=None, net_map={},
            nets_routed=0, nets_to_route=20, completion=0.0, success=False,
            stats={"segments": 10, "vias": 1},
        )

        assert _is_better_result(better, worse) is True
        assert _is_better_result(worse, better) is False


class TestTiebreakerInRuleRelaxation:
    """Integration: verify tiebreaker selects best result in rule relaxation."""

    def test_early_stop_does_not_trigger_on_tied_completion(self):
        """When all completions are 0.0, early-stop should not trigger."""
        from kicad_tools.cli.route_cmd import route_with_rule_relaxation

        tiers = [
            FakeTier(0, "user", 0.2, 0.15),
            FakeTier(1, "moderate", 0.15, 0.127),
            FakeTier(2, "aggressive", 0.127, 0.1),
        ]

        # All tie at 0% completion
        routers = [
            _make_fake_router(0, 20, segments=10),
            _make_fake_router(0, 20, segments=20),
            _make_fake_router(0, 20, segments=30),
        ]
        router_idx = [0]

        def fake_load_pcb(*args, **kwargs):
            r = routers[router_idx[0]]
            router_idx[0] += 1
            return r, {"net1": 1}

        args = _make_args()  # early_stop enabled (default)

        with _patches_for_relaxation(tiers, fake_load_pcb):
            route_with_rule_relaxation(
                pcb_path=MagicMock(),
                output_path=MagicMock(),
                args=args,
                quiet=True,
            )

        # All 3 tiers should run (no regression in completion, all 0.0)
        assert router_idx[0] == 3
