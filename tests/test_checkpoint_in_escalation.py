"""Regression tests for Issue #3051 -- checkpoint_callback wired into
layer-escalation / rule-relaxation / combined-escalation paths.

Background
----------

PR #3056's judge confirmed that the iteration-0 ``checkpoint_callback``
plumbing introduced for Issue #2808 only fires on the single-attempt
``kct route`` path.  The three escalation wrappers
(``route_with_layer_escalation``, ``route_with_rule_relaxation``,
``route_with_combined_escalation``) called ``route_all_negotiated``
WITHOUT passing the callback, so any kill mid-route (SIGTERM /
timeout-then-kill) discarded the best-so-far snapshot for every run that
took the escalation path.

This file asserts -- via mocking ``route_all_negotiated`` -- that each
escalation wrapper now forwards a non-None ``checkpoint_callback`` to
every per-attempt routing call.  The mock returns immediately so the
test runs in milliseconds while still exercising the parameter-passing
contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_args(**overrides):
    """Build a SimpleNamespace mimicking parsed argparse args.

    Default checkpoint_interval is 30.0 (matches the route_cmd parser
    default at L4729), which causes _make_checkpoint_callback to return
    a non-None callback.
    """
    defaults = {
        "backend": "python",
        "grid": 0.25,
        "trace_width": 0.2,
        "clearance": 0.15,
        "via_drill": 0.3,
        "via_diameter": 0.6,
        "fine_pitch_clearance": None,
        "skip_nets": None,
        "auto_pour": False,
        "max_layers": 6,
        "min_completion": 0.95,
        "strategy": "negotiated",
        "verbose": False,
        "force": False,
        "timeout": 60,
        "iterations": 3,
        "per_net_timeout": None,
        "batch_routing": False,
        "high_performance": False,
        "hierarchical": False,
        "perturbation": True,
        "two_phase": False,
        "multi_resolution": False,
        "edge_clearance": 0.25,
        "escape_routing": None,
        "no_optimize": True,
        "dry_run": True,
        # Issue #3051: required for callback build.
        "checkpoint_interval": 30.0,
        # Adaptive-rules / mfr-tier specific defaults so the rule and
        # combined-escalation paths work with the same helper.
        "manufacturer": "jlcpcb",
        "min_trace": 0.075,
        "min_clearance_floor": 0.075,
        "layers": "2",
        "no_early_stop": False,
        "no_auto_build_native": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_mock_router(nets_routed: int, nets_to_route: int, overflow: int):
    """Mock Autorouter sufficient for the escalation loops to traverse."""
    router = MagicMock()
    router.nets = {i: [f"pad{j}" for j in range(2)] for i in range(1, nets_to_route + 1)}
    router.grid.width = 50.0
    router.grid.height = 40.0
    router.grid.get_total_overflow.return_value = overflow
    router.get_statistics.return_value = {
        "nets_routed": nets_routed,
        "segments": 10,
        "vias": 2,
    }
    router.power_stall_abort = False
    router._pour_nets_without_zones = set()
    router.rules.via_diameter = 0.6
    router.rules.min_drill_clearance = 0.0
    router.rules.trace_width = 0.2
    router.rules.trace_clearance = 0.15
    router.routes = []
    return router


# =============================================================================
# Layer escalation path -- route_with_layer_escalation @ ~L2337
# =============================================================================


class TestCheckpointForwardedInLayerEscalation:
    """The layer-escalation per-attempt loop must forward
    ``checkpoint_callback`` so a kill mid-loop persists the best snapshot.
    """

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_layer_escalation_forwards_checkpoint_callback(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Issue #3051: route_with_layer_escalation must pass a non-None
        ``checkpoint_callback`` to every ``route_all_negotiated`` call."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Router that converges on first attempt so we don't loop forever.
        router = _make_mock_router(nets_routed=3, nets_to_route=3, overflow=0)

        def mock_load(*args, **kwargs):
            return router, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_layer_escalation(
                            pcb, out, _make_args(), quiet=True
                        )

        # Inspect the call: route_all_negotiated must have been called
        # with a non-None checkpoint_callback kwarg.
        assert router.route_all_negotiated.called, (
            "route_all_negotiated should have been called"
        )
        call = router.route_all_negotiated.call_args
        assert "checkpoint_callback" in call.kwargs, (
            "Issue #3051: route_with_layer_escalation must pass "
            "checkpoint_callback= to route_all_negotiated; got kwargs="
            f"{list(call.kwargs.keys())}"
        )
        assert callable(call.kwargs["checkpoint_callback"]), (
            "Issue #3051: checkpoint_callback must be a callable "
            "(non-None) when --checkpoint-interval > 0; "
            f"got {type(call.kwargs['checkpoint_callback'])!r}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_layer_escalation_no_callback_when_interval_zero(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """When ``--checkpoint-interval 0`` is set, the callback factory
        returns None and the router receives None (no checkpointing).
        This proves the opt-out path still works."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        router = _make_mock_router(nets_routed=3, nets_to_route=3, overflow=0)

        def mock_load(*args, **kwargs):
            return router, {}

        args = _make_args(checkpoint_interval=0.0)

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_layer_escalation(pcb, out, args, quiet=True)

        call = router.route_all_negotiated.call_args
        # Either explicit None or absent are both valid opt-out states.
        cb = call.kwargs.get("checkpoint_callback")
        assert cb is None, (
            "checkpoint_callback should be None when --checkpoint-interval 0; "
            f"got {cb!r}"
        )


# =============================================================================
# Rule relaxation path -- route_with_rule_relaxation @ ~L3053
# =============================================================================


class TestCheckpointForwardedInRuleRelaxation:
    """The rule-relaxation per-tier loop must forward
    ``checkpoint_callback``.
    """

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_rule_relaxation_forwards_checkpoint_callback(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Issue #3051: route_with_rule_relaxation must pass a non-None
        ``checkpoint_callback`` to ``route_all_negotiated``."""
        from kicad_tools.cli.route_cmd import route_with_rule_relaxation

        pcb = tmp_path / "test.kicad_pcb"
        # Provide a minimal PCB that can be parsed for the layer-stack
        # auto-detection branch (layers="2" forces the map branch instead).
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        router = _make_mock_router(nets_routed=3, nets_to_route=3, overflow=0)

        def mock_load(*args, **kwargs):
            return router, {}

        args = _make_args(layers="2")

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_rule_relaxation(pcb, out, args, quiet=True)

        assert router.route_all_negotiated.called, (
            "route_all_negotiated should have been called in the "
            "rule-relaxation path"
        )
        call = router.route_all_negotiated.call_args
        assert "checkpoint_callback" in call.kwargs, (
            "Issue #3051: route_with_rule_relaxation must pass "
            "checkpoint_callback= to route_all_negotiated; got kwargs="
            f"{list(call.kwargs.keys())}"
        )
        assert callable(call.kwargs["checkpoint_callback"]), (
            "Issue #3051: checkpoint_callback must be a callable "
            "(non-None) when --checkpoint-interval > 0; "
            f"got {type(call.kwargs['checkpoint_callback'])!r}"
        )


# =============================================================================
# Combined (2D) escalation path -- route_with_combined_escalation @ ~L4131
# =============================================================================


class TestCheckpointForwardedInCombinedEscalation:
    """The combined-escalation 2D search must forward
    ``checkpoint_callback``."""

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_combined_escalation_forwards_checkpoint_callback(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Issue #3051: route_with_combined_escalation must pass a non-None
        ``checkpoint_callback`` to ``route_all_negotiated``."""
        from kicad_tools.cli.route_cmd import route_with_combined_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        router = _make_mock_router(nets_routed=3, nets_to_route=3, overflow=0)

        def mock_load(*args, **kwargs):
            return router, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_combined_escalation(
                            pcb, out, _make_args(), quiet=True
                        )

        assert router.route_all_negotiated.called, (
            "route_all_negotiated should have been called in the "
            "combined-escalation path"
        )
        call = router.route_all_negotiated.call_args
        assert "checkpoint_callback" in call.kwargs, (
            "Issue #3051: route_with_combined_escalation must pass "
            "checkpoint_callback= to route_all_negotiated; got kwargs="
            f"{list(call.kwargs.keys())}"
        )
        assert callable(call.kwargs["checkpoint_callback"]), (
            "Issue #3051: checkpoint_callback must be a callable "
            "(non-None) when --checkpoint-interval > 0; "
            f"got {type(call.kwargs['checkpoint_callback'])!r}"
        )


# =============================================================================
# End-to-end: mocked checkpoint fires and produces a non-empty PCB
# =============================================================================


class TestCheckpointActuallyWritesInEscalation:
    """End-to-end sanity: when the mocked route_all_negotiated INVOKES the
    forwarded callback (simulating an iteration-0 best-so-far event),
    the output PCB ends up with at least one segment on disk -- proving
    the kill-mid-loop path now persists work."""

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_layer_escalation_callback_invocation_writes_to_disk(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Mock route_all_negotiated to invoke the forwarded callback with a
        single route.  Assert the output PCB on disk has at least one
        ``(segment`` after the callback fires -- this is the kill-mid-loop
        recovery scenario from Issue #3051."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation
        from kicad_tools.core.types import CopperLayer
        from kicad_tools.router.core import IterationMetrics
        from kicad_tools.router.primitives import Route, Segment

        # Minimal 2L input PCB so the checkpoint writer can splice into it.
        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(
            "(kicad_pcb\n"
            "  (version 20240108)\n"
            "  (generator pcbnew)\n"
            "  (layers\n"
            '    (0 "F.Cu" signal)\n'
            '    (31 "B.Cu" signal)\n'
            "  )\n"
            ")\n"
        )
        out = tmp_path / "out.kicad_pcb"

        # Build a "real" route the callback will receive when invoked.
        sample_route = Route(
            net=1,
            net_name="NET1",
            segments=[
                Segment(
                    x1=0.0,
                    y1=0.0,
                    x2=1.0,
                    y2=1.0,
                    width=0.2,
                    layer=CopperLayer.F_CU,
                    net=1,
                ),
            ],
        )

        router = _make_mock_router(nets_routed=3, nets_to_route=3, overflow=0)

        # Custom route_all_negotiated that invokes the forwarded
        # checkpoint_callback before returning.  Simulates the iteration-0
        # best-so-far snapshot landing on disk before a hypothetical kill.
        def _fake_route_all_negotiated(*args, **kwargs):
            cb = kwargs.get("checkpoint_callback")
            if cb is not None:
                cb([sample_route], IterationMetrics(iteration=1, routed_count=1, overflow=0))
            return []

        router.route_all_negotiated.side_effect = _fake_route_all_negotiated

        def mock_load(*args, **kwargs):
            return router, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_layer_escalation(
                            pcb, out, _make_args(), quiet=True
                        )

        # Output file exists AND contains at least one segment.  Pre-fix:
        # callback was never forwarded, so this assertion failed with 0
        # segments (or no file at all).
        assert out.exists(), (
            "Issue #3051: output PCB must exist after callback invocation"
        )
        segment_count = out.read_text().count("(segment")
        assert segment_count >= 1, (
            f"Issue #3051: expected >=1 (segment after checkpoint, got "
            f"{segment_count}.  This is the kill-mid-loop regression."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
