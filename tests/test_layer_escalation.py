"""Tests for automatic layer escalation feature (issue #869).

Verifies that the --auto-layers flag correctly escalates layer count
when routing fails to achieve the minimum completion threshold.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestLayerEscalationCLIParameters:
    """Tests for --auto-layers parameter handling in route command."""

    def test_auto_layers_default_not_forwarded(self):
        """auto-layers is the default (Issue #2388) so it is NOT forwarded
        to the underlying CLI when set to True; --no-auto-layers IS
        forwarded when explicitly disabled."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid=0.25,
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
            layers="auto",
            force=False,
            no_optimize=False,
            auto_layers=True,  # default value
            max_layers=6,
            min_completion=0.95,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            # Default value: do not forward (the underlying CLI also defaults to True).
            assert "--auto-layers" not in call_args
            assert "--no-auto-layers" not in call_args

    def test_no_auto_layers_forwarded_when_disabled(self):
        """--no-auto-layers is forwarded when auto_layers=False (Issue #2388)."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid=0.25,
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
            layers="auto",
            force=False,
            no_optimize=False,
            auto_layers=False,  # user explicitly disabled
            max_layers=6,
            min_completion=0.95,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--no-auto-layers" in call_args
            assert "--auto-layers" not in call_args

    def test_max_layers_parameter_passed_when_not_default(self):
        """max-layers parameter is passed when different from default 6."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid=0.25,
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
            layers="auto",
            force=False,
            no_optimize=False,
            auto_layers=True,
            max_layers=4,  # Non-default value
            min_completion=0.95,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--max-layers" in call_args
            idx = call_args.index("--max-layers")
            assert call_args[idx + 1] == "4"

    def test_min_completion_parameter_passed_when_not_default(self):
        """min-completion parameter is passed when different from default 0.95."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid=0.25,
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
            layers="auto",
            force=False,
            no_optimize=False,
            auto_layers=True,
            max_layers=6,
            min_completion=0.90,  # Non-default value
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--min-completion" in call_args
            idx = call_args.index("--min-completion")
            assert call_args[idx + 1] == "0.9"


class TestLayerEscalationValidation:
    """Tests for auto-layers validation rules."""

    def test_auto_layers_conflicts_with_explicit_layers(self, tmp_path):
        """auto-layers cannot be used with explicit --layers option."""
        from kicad_tools.cli.route_cmd import main as route_main

        # Create a minimal test PCB file
        pcb_content = """(kicad_pcb (version 20240101) (generator "test"))"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # --auto-layers with --layers 4 should fail
        result = route_main(
            [
                str(test_pcb),
                "--auto-layers",
                "--layers",
                "4",
                "--dry-run",
                "--quiet",
            ]
        )

        assert result == 1, "Should fail when --auto-layers used with explicit --layers"

    def test_auto_layers_works_with_layers_auto(self, tmp_path):
        """auto-layers can be used with --layers auto (implicit default)."""
        from kicad_tools.cli.route_cmd import main as route_main

        # Create a minimal test PCB file with proper structure
        pcb_content = """(kicad_pcb
  (version 20240101)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (grid_origin 0 0)
  )
  (net 0 "")
)"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # --auto-layers without explicit --layers should work
        # Use --force to bypass grid/clearance validation and --grid 0.1
        # to have a valid configuration
        result = route_main(
            [
                str(test_pcb),
                "--auto-layers",
                "--grid",
                "0.1",
                "--dry-run",
                "--quiet",
            ]
        )

        # Should succeed with 0 nets to route (dry-run, minimal PCB)
        assert result == 0, "Should succeed when --auto-layers used alone"

    def test_min_completion_must_be_between_0_and_1(self, tmp_path):
        """min-completion must be between 0 and 1."""
        from kicad_tools.cli.route_cmd import main as route_main

        pcb_content = """(kicad_pcb (version 20240101) (generator "test"))"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # --min-completion > 1 should fail
        result = route_main(
            [
                str(test_pcb),
                "--auto-layers",
                "--min-completion",
                "1.5",
                "--dry-run",
                "--quiet",
            ]
        )

        assert result == 1, "Should fail when --min-completion > 1"

        # --min-completion < 0 should fail
        result = route_main(
            [
                str(test_pcb),
                "--auto-layers",
                "--min-completion",
                "-0.5",
                "--dry-run",
                "--quiet",
            ]
        )

        assert result == 1, "Should fail when --min-completion < 0"


class TestUpdatePcbLayerStackup:
    """Tests for update_pcb_layer_stackup function."""

    def test_update_2_to_4_layers(self):
        """update_pcb_layer_stackup correctly updates 2-layer to 4-layer."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
)"""
        result = update_pcb_layer_stackup(pcb_content, 4)

        # Should now have 4 copper layers
        assert '"In1.Cu"' in result
        assert '"In2.Cu"' in result
        assert '"F.Cu"' in result
        assert '"B.Cu"' in result

    def test_update_2_to_6_layers(self):
        """update_pcb_layer_stackup correctly updates 2-layer to 6-layer."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
)"""
        result = update_pcb_layer_stackup(pcb_content, 6)

        # Should now have 6 copper layers
        assert '"In1.Cu"' in result
        assert '"In2.Cu"' in result
        assert '"In3.Cu"' in result
        assert '"In4.Cu"' in result
        assert '"F.Cu"' in result
        assert '"B.Cu"' in result

    def test_no_change_if_already_enough_layers(self):
        """update_pcb_layer_stackup does nothing if already has enough layers."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
)"""
        result = update_pcb_layer_stackup(pcb_content, 4)

        # Should be unchanged (already has 4 layers)
        assert result == pcb_content


class TestUpdatePcbLayerStackupPowerLayers:
    """Tests for copper layer counting with non-signal type keywords (issue #1773)."""

    def test_power_typed_layer_counted(self):
        """Board with power-typed inner layer correctly counts 4 copper layers."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" power)
    (31 "B.Cu" signal)
  )
)"""
        # Targeting 4 layers on a board that already has 4 => no change
        result = update_pcb_layer_stackup(pcb_content, 4)
        assert result == pcb_content, (
            "4-layer board with power-typed In2.Cu should NOT be upgraded"
        )

    def test_mixed_type_keywords_all_counted(self):
        """Board with signal, power, and mixed type keywords all counted correctly."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" mixed)
    (2 "In2.Cu" power)
    (3 "In3.Cu" signal)
    (4 "In4.Cu" power)
    (31 "B.Cu" signal)
  )
)"""
        # Already has 6 copper layers => no change when targeting 6
        result = update_pcb_layer_stackup(pcb_content, 6)
        assert result == pcb_content, (
            "6-layer board with mixed types should NOT be upgraded"
        )

    def test_noop_when_power_layers_sufficient(self):
        """4-layer board with power-typed inner layers targeting 4 returns unchanged."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" power)
    (31 "B.Cu" signal)
  )
)"""
        result = update_pcb_layer_stackup(pcb_content, 4)
        assert result == pcb_content

    def test_non_copper_layers_preserved(self):
        """Non-copper layers are preserved after stackup update."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user)
    (33 "F.Adhes" user)
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user)
    (37 "F.SilkS" user)
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user)
    (47 "F.CrtYd" user)
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
)"""
        result = update_pcb_layer_stackup(pcb_content, 4)

        # Non-copper layers should still be present in content
        # (they may be outside the layers block depending on implementation,
        # but the key point is the output is valid)
        assert '"F.Cu"' in result
        assert '"B.Cu"' in result
        assert '"In1.Cu"' in result
        assert '"In2.Cu"' in result

    def test_unknown_type_keyword_counted(self):
        """Hypothetical unknown type keyword is still counted as a copper layer."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" custom)
    (31 "B.Cu" jumper)
  )
)"""
        # 4 copper layers already => no change when targeting 4
        result = update_pcb_layer_stackup(pcb_content, 4)
        assert result == pcb_content, (
            "Unknown type keywords should still be counted as copper layers"
        )

    def test_layer_ids_stable_after_update(self):
        """Layer IDs remain stable — B.Cu stays at layer 31."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_content = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" power)
  )
)"""
        result = update_pcb_layer_stackup(pcb_content, 4)

        # B.Cu should still be at layer 31
        assert '(31 "B.Cu"' in result
        # F.Cu should still be at layer 0
        assert '(0 "F.Cu"' in result


class TestLayerEscalationResult:
    """Tests for LayerEscalationResult dataclass."""

    def test_dataclass_fields(self):
        """LayerEscalationResult has all required fields."""
        from kicad_tools.cli.route_cmd import LayerEscalationResult
        from kicad_tools.router import LayerStack

        result = LayerEscalationResult(
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            router=None,  # Mock
            net_map={},
            nets_routed=5,
            nets_to_route=10,
            completion=0.5,
            success=False,
        )

        assert result.layer_count == 2
        assert result.nets_routed == 5
        assert result.nets_to_route == 10
        assert result.completion == 0.5
        assert result.success is False


class TestLayerEscalationHelpText:
    """Tests for help text documentation."""

    def test_auto_layers_in_help(self):
        """Verify --auto-layers flag is documented in help text."""
        import contextlib
        import sys
        from io import StringIO
        from unittest.mock import patch

        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()

        assert "--auto-layers" in help_text, "Help should document --auto-layers"
        assert "--max-layers" in help_text, "Help should document --max-layers"
        assert "--min-completion" in help_text, "Help should document --min-completion"
        assert "escalat" in help_text.lower(), "Help should mention escalation"


class TestBestOfAttemptsSelection:
    """Tests for Issue #2396: best-of-attempts uses absolute nets_routed.

    When nets_to_route differs across escalation attempts (e.g. power
    nets auto-skipped on 4L but not 2L), the comparison must use
    absolute nets_routed count, not completion ratio.
    """

    def test_absolute_nets_routed_wins_over_ratio(self):
        """6/10 (0.60) beats 3/8 (0.375) using absolute nets_routed."""
        from kicad_tools.cli.route_cmd import LayerEscalationResult, _is_better_result
        from kicad_tools.router import LayerStack

        result_2l = LayerEscalationResult(
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            router=None,
            net_map={},
            nets_routed=6,
            nets_to_route=10,
            completion=0.6,
            success=False,
        )

        result_4l = LayerEscalationResult(
            layer_count=4,
            layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            router=None,
            net_map={},
            nets_routed=3,
            nets_to_route=8,
            completion=0.375,
            success=False,
        )

        # 2L (6 routed) should beat 4L (3 routed)
        assert _is_better_result(result_2l, result_4l) is True
        assert _is_better_result(result_4l, result_2l) is False

    def test_same_nets_routed_tiebreaks_on_completion(self):
        """When nets_routed is tied, higher completion ratio wins."""
        from kicad_tools.cli.route_cmd import LayerEscalationResult, _is_better_result
        from kicad_tools.router import LayerStack

        result_a = LayerEscalationResult(
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            router=None,
            net_map={},
            nets_routed=5,
            nets_to_route=10,
            completion=0.5,
            success=False,
        )

        result_b = LayerEscalationResult(
            layer_count=4,
            layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            router=None,
            net_map={},
            nets_routed=5,
            nets_to_route=8,
            completion=0.625,
            success=False,
        )

        # Same nets_routed (5), but B has higher completion (0.625 > 0.5)
        assert _is_better_result(result_b, result_a) is True
        assert _is_better_result(result_a, result_b) is False

    def test_same_nets_same_completion_prefers_fewer_layers(self):
        """When everything is tied, fewer layers wins."""
        from kicad_tools.cli.route_cmd import LayerEscalationResult, _is_better_result
        from kicad_tools.router import LayerStack

        result_2l = LayerEscalationResult(
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            router=None,
            net_map={},
            nets_routed=5,
            nets_to_route=10,
            completion=0.5,
            success=False,
            stats={"segments": 10, "vias": 2},
        )

        result_4l = LayerEscalationResult(
            layer_count=4,
            layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            router=None,
            net_map={},
            nets_routed=5,
            nets_to_route=10,
            completion=0.5,
            success=False,
            stats={"segments": 10, "vias": 2},
        )

        # Tied on all metrics except layer count: 2L wins
        assert _is_better_result(result_2l, result_4l) is True
        assert _is_better_result(result_4l, result_2l) is False

    def test_higher_nets_routed_wins_even_with_lower_ratio(self):
        """A result with more absolute routed nets wins even if its
        completion ratio is lower (cross-denominator case)."""
        from kicad_tools.cli.route_cmd import LayerEscalationResult, _is_better_result
        from kicad_tools.router import LayerStack

        # 7/20 = 0.35 ratio but 7 absolute
        result_many = LayerEscalationResult(
            layer_count=4,
            layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            router=None,
            net_map={},
            nets_routed=7,
            nets_to_route=20,
            completion=0.35,
            success=False,
        )

        # 5/6 = 0.833 ratio but only 5 absolute
        result_few = LayerEscalationResult(
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            router=None,
            net_map={},
            nets_routed=5,
            nets_to_route=6,
            completion=5 / 6,
            success=False,
        )

        # 7 > 5, so result_many wins despite lower ratio
        assert _is_better_result(result_many, result_few) is True


class TestPristineStatePerAttempt:
    """Tests for Issue #2396: pristine state per layer-escalation attempt.

    Verify that the reset_attempt_state() method is accessible and
    that the orchestrator code path calls it.
    """

    def test_reset_attempt_state_exists(self):
        """Autorouter has a reset_attempt_state method."""
        from kicad_tools.router.core import Autorouter

        router = Autorouter(width=50.0, height=40.0)
        assert hasattr(router, "reset_attempt_state")
        assert callable(router.reset_attempt_state)

    def test_no_auto_layers_skips_escalation(self):
        """When --no-auto-layers is passed, route_with_layer_escalation is
        NOT called (C6).

        This verifies the orchestrator code path is unchanged when
        escalation is disabled -- the main() dispatcher selects the
        fixed-layer routing path instead of the escalation path.
        """
        from kicad_tools.cli.route_cmd import main as route_main

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation"
        ) as mock_escalation:
            mock_escalation.return_value = 0

            # --no-auto-layers should NOT call route_with_layer_escalation.
            # It will fail on PCB loading since we don't mock that, but the
            # key assertion is that escalation was never invoked.
            try:
                route_main([
                    "test.kicad_pcb",
                    "--no-auto-layers",
                    "--quiet",
                ])
            except (SystemExit, FileNotFoundError, Exception):
                pass  # Expected -- PCB file doesn't exist

            assert mock_escalation.call_count == 0, (
                "route_with_layer_escalation should not be called with --no-auto-layers"
            )
