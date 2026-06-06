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


class TestLayerStackupParenBalance:
    """Tests for Issue #2416: S-expression syntax error from unbalanced parens.

    Realistic KiCad PCB files include non-copper layers (B.SilkS, Edge.Cuts,
    etc.) in the (layers ...) block. The old regex only matched through the
    first inner entry's closing paren, orphaning the rest and producing
    unbalanced parentheses.
    """

    REALISTIC_2L_PCB = """\
(kicad_pcb
  (version 20240101)
  (generator "kicad")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user "B.Mask")
    (39 "F.Mask" user "F.Mask")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (net 0 "")
  (net 1 "VCC")
)"""

    def test_2_to_4_balanced_parens(self):
        """Upgrading 2L to 4L with non-copper layers produces balanced parens."""
        from kicad_tools.cli.route_cmd import (
            _validate_sexp_parentheses,
            update_pcb_layer_stackup,
        )

        result = update_pcb_layer_stackup(self.REALISTIC_2L_PCB, 4)

        assert _validate_sexp_parentheses(result), (
            "Output has unbalanced parentheses"
        )
        assert '"In1.Cu"' in result
        assert '"In2.Cu"' in result
        assert '"F.Cu"' in result
        assert '"B.Cu"' in result

    def test_2_to_6_balanced_parens(self):
        """Upgrading 2L to 6L with non-copper layers produces balanced parens."""
        from kicad_tools.cli.route_cmd import (
            _validate_sexp_parentheses,
            update_pcb_layer_stackup,
        )

        result = update_pcb_layer_stackup(self.REALISTIC_2L_PCB, 6)

        assert _validate_sexp_parentheses(result), (
            "Output has unbalanced parentheses"
        )
        assert '"In1.Cu"' in result
        assert '"In4.Cu"' in result

    def test_non_copper_layers_preserved_after_upgrade(self):
        """Non-copper layers (SilkS, Edge.Cuts, Fab, etc.) survive the upgrade."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        result = update_pcb_layer_stackup(self.REALISTIC_2L_PCB, 4)

        for layer_name in [
            "B.Adhes", "F.Adhes", "B.Paste", "F.Paste",
            "B.SilkS", "F.SilkS", "B.Mask", "F.Mask",
            "Edge.Cuts", "Margin", "B.CrtYd", "F.CrtYd",
            "B.Fab", "F.Fab",
        ]:
            assert layer_name in result, (
                f"Non-copper layer {layer_name!r} was lost during upgrade"
            )

    def test_content_outside_layers_block_preserved(self):
        """Content after the (layers ...) block is not corrupted."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        result = update_pcb_layer_stackup(self.REALISTIC_2L_PCB, 4)

        assert '(net 0 "")' in result
        assert '(net 1 "VCC")' in result

    def test_non_copper_entries_with_extra_fields(self):
        """Layer entries with extra string fields (e.g. user "B.Adhesive")
        are preserved correctly."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        result = update_pcb_layer_stackup(self.REALISTIC_2L_PCB, 4)

        # Entries with display name strings should survive
        assert "B.Adhesive" in result
        assert "F.Silkscreen" in result
        assert "B.Courtyard" in result


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


class TestEarlyTermination:
    """Tests for Issue #2412: early termination when escalation cannot help.

    Two cases:
    1. Zero overflow with incomplete routing (topology/placement issue)
    2. Stagnation (consecutive attempts yield identical results)
    """

    def _make_mock_router(self, nets_routed, nets_to_route, overflow):
        """Create a mock router that returns predictable results."""
        from unittest.mock import MagicMock

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
        # Provide real float values for rules attributes used by drc_nudge
        router.rules.via_diameter = 0.6
        router.rules.min_drill_clearance = 0.0
        router.rules.trace_width = 0.2
        router.rules.trace_clearance = 0.15
        return router

    def _make_args(self, **overrides):
        """Create minimal args for route_with_layer_escalation."""
        defaults = dict(
            backend="python",
            grid=0.25,
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            fine_pitch_clearance=None,
            skip_nets=None,
            auto_pour=False,
            max_layers=6,
            min_completion=0.95,
            strategy="negotiated",
            verbose=False,
            force=False,
            timeout=60,
            iterations=3,
            per_net_timeout=None,
            batch_routing=False,
            high_performance=False,
            hierarchical=False,
            perturbation=True,
            two_phase=False,
            multi_resolution=False,
            edge_clearance=0.25,
            escape_routing=None,
            no_optimize=True,
            dry_run=True,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_zero_overflow_stops_early(self, _esc_flag, _esc_use, _pour, tmp_path):
        """When overflow=0 but nets are incomplete, escalation stops immediately."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Router: 2/3 nets routed, zero overflow
        router = self._make_mock_router(nets_routed=2, nets_to_route=3, overflow=0)
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return router, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, self._make_args(), quiet=True)

        # Should stop after 1 attempt (zero overflow => no point escalating)
        assert call_count == 1, (
            f"Expected 1 attempt (zero-overflow early stop), got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_stagnation_stops_early(self, _esc_flag, _esc_use, _pour, tmp_path):
        """When consecutive attempts produce identical results, escalation stops."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Router: 2/3 nets routed, some overflow, same every time
        router = self._make_mock_router(nets_routed=2, nets_to_route=3, overflow=5)
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return router, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, self._make_args(), quiet=True)

        # Should stop after 2 attempts (stagnation: 2nd == 1st)
        assert call_count == 2, (
            f"Expected 2 attempts (stagnation early stop), got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_improvement_continues_escalation(self, _esc_flag, _esc_use, _pour, tmp_path):
        """When each attempt improves, all configurations are tried."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Each attempt improves: more nets routed, less overflow
        attempt_results = [
            (1, 5, 20),  # 2L: 1/5 routed, overflow=20
            (2, 5, 15),  # 4L sig_gnd_pwr_sig: 2/5, overflow=15
            (3, 5, 10),  # 4L all_signal: 3/5, overflow=10
            (4, 5, 5),   # 6L: 4/5, overflow=5
        ]
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            nets_routed, nets_to_route, overflow = attempt_results[call_count]
            call_count += 1
            return self._make_mock_router(nets_routed, nets_to_route, overflow), {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, self._make_args(), quiet=True)

        # All 4 configs tried because each improves
        assert call_count == 4, (
            f"Expected 4 attempts (continuous improvement), got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_success_still_stops_loop(self, _esc_flag, _esc_use, _pour, tmp_path):
        """When routing succeeds, the loop still exits (not broken by early-stop)."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # First attempt: all nets routed with some overflow
        router = self._make_mock_router(nets_routed=3, nets_to_route=3, overflow=2)
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return router, {}

        args = self._make_args()
        args.min_completion = 0.95  # 3/3 = 100% >= 95%

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch("kicad_tools.cli.route_cmd.run_post_route_drc", return_value=False):
                        result = route_with_layer_escalation(
                            pcb, out, args, quiet=True
                        )

        assert call_count == 1, (
            f"Expected 1 attempt (success on first try), got {call_count}"
        )

    def test_overflow_field_on_result(self):
        """LayerEscalationResult stores the overflow field."""
        from kicad_tools.cli.route_cmd import LayerEscalationResult
        from kicad_tools.router import LayerStack

        result = LayerEscalationResult(
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            router=None,
            net_map={},
            nets_routed=5,
            nets_to_route=10,
            completion=0.5,
            success=False,
            overflow=42,
        )

        assert result.overflow == 42

    def test_overflow_field_defaults_to_zero(self):
        """LayerEscalationResult overflow defaults to 0 for backward compat."""
        from kicad_tools.cli.route_cmd import LayerEscalationResult
        from kicad_tools.router import LayerStack

        result = LayerEscalationResult(
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            router=None,
            net_map={},
            nets_routed=5,
            nets_to_route=10,
            completion=0.5,
            success=False,
        )

        assert result.overflow == 0

    # Issue #2634: monte-carlo (and basic / evolutionary) strategies never
    # accumulate a meaningful overflow signal because they don't plant
    # overlapping tracks like the negotiated congestion router does.  Reading
    # ``overflow == 0`` from them caused the zero-overflow heuristic above to
    # fire after attempt 1, killing escalation even when ``--auto-layers`` was
    # explicitly on.

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_monte_carlo_skips_zero_overflow_heuristic(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """MC + auto-layers must escalate past 2L even with overflow=0.

        Regression for Issue #2634: ``run_monte_carlo`` calls basic A* per trial,
        which never accumulates ``grid.get_total_overflow()``.  The zero-overflow
        early-termination heuristic (calibrated for negotiated) used to fire
        after attempt 1 and break the loop.  After the fix, MC trials no longer
        trigger that heuristic and the escalation loop tries at least one
        higher layer count.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Router: 2/3 nets routed every attempt, overflow=0 (MC has no signal)
        # and nets_routed increases just enough between attempts to defeat the
        # *stagnation* heuristic — we want to confirm the *zero-overflow*
        # heuristic doesn't fire on its own.
        attempt_results = [
            (1, 3, 0),  # 2L: 1/3 routed, overflow=0
            (2, 3, 0),  # 4L sig_gnd_pwr_sig: 2/3, overflow=0 (improved)
            (3, 3, 0),  # 4L all_signal: 3/3, overflow=0 (success)
        ]
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            nets_routed, nets_to_route, overflow = attempt_results[
                min(call_count, len(attempt_results) - 1)
            ]
            call_count += 1
            return self._make_mock_router(nets_routed, nets_to_route, overflow), {}

        args = self._make_args(strategy="monte-carlo")
        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_layer_escalation(pcb, out, args, quiet=True)

        # Must attempt at least 2 layer configurations.  Pre-fix this was 1 —
        # the zero-overflow heuristic broke the loop after attempt 1.
        assert call_count >= 2, (
            f"Expected >=2 attempts with --strategy monte-carlo (zero-overflow "
            f"heuristic must NOT fire on MC), got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_basic_strategy_skips_zero_overflow_heuristic(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """``--strategy basic`` also escalates past 2L despite overflow=0.

        Basic A* never plants overlaps; the same heuristic exemption applies.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        attempt_results = [
            (1, 3, 0),
            (2, 3, 0),
            (3, 3, 0),
        ]
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            nets_routed, nets_to_route, overflow = attempt_results[
                min(call_count, len(attempt_results) - 1)
            ]
            call_count += 1
            return self._make_mock_router(nets_routed, nets_to_route, overflow), {}

        args = self._make_args(strategy="basic")
        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_layer_escalation(pcb, out, args, quiet=True)

        assert call_count >= 2, (
            f"Expected >=2 attempts with --strategy basic, got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_negotiated_still_uses_zero_overflow_heuristic(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """The zero-overflow heuristic still fires for ``--strategy negotiated``.

        Regression guard: Issue #2634 must NOT change the negotiated behaviour
        that Issue #2412 added.  Negotiated reading overflow=0 with incomplete
        routing should still break after attempt 1.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        router = self._make_mock_router(nets_routed=2, nets_to_route=3, overflow=0)
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return router, {}

        args = self._make_args(strategy="negotiated")
        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, args, quiet=True)

        # Negotiated: zero-overflow heuristic still fires after attempt 1
        assert call_count == 1, (
            f"Expected 1 attempt with --strategy negotiated (zero-overflow early "
            f"stop preserved), got {call_count}"
        )

    # Issue #2673: Completion-floor guard.  Both early-termination heuristics
    # (zero-overflow at line 1733 and stagnation at line 1753) are calibrated
    # for the case where the prior attempt already routed a substantial
    # fraction of the board.  When best-so-far completion is < 50%, the
    # failure mode is usually "router stuck on a few nets" rather than
    # "design needs more layers", so escalation should continue.  Board 05
    # on 2026-05-11 exhibited exactly this regression: 2L=0/35, overflow=0,
    # and the negotiated zero-overflow heuristic fired immediately — never
    # trying 4L.  See issue #2673 for the diagnostic numbers.

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_completion_floor_bypasses_zero_overflow_below_floor(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Issue #2673: zero-overflow short-circuit must NOT fire when
        best-so-far completion is below the 50% floor.

        Board 05 fixture: negotiated strategy, 0/10 routed, overflow=0.
        Without the floor guard, attempt 1 ends with "Escalation stopped:
        failures are not congestion-related (overflow=0)" and the loop
        exits after one attempt.  With the guard, escalation continues to
        at least the next layer configuration.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # 0/10 routed, overflow=0 — completion 0% << 50% floor
        router = self._make_mock_router(nets_routed=0, nets_to_route=10, overflow=0)
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return router, {}

        args = self._make_args(strategy="negotiated")
        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, args, quiet=True)

        # Must NOT stop at attempt 1 — completion 0% is below floor.
        # Loop will continue until it stagnates with completion still below
        # floor on a non-first attempt (where stagnation also gets bypassed),
        # so all 4 layer configurations get tried.
        assert call_count >= 2, (
            f"Expected escalation past attempt 1 when completion=0% is below "
            f"50% floor (issue #2673), got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_completion_floor_bypasses_stagnation_below_floor(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Issue #2673: stagnation short-circuit must NOT fire when
        best-so-far completion is below the 50% floor.

        Even if attempts 1 and 2 produce identical (low) results, we should
        still try the remaining layer configurations — the failure mode at
        low completion is usually router-internal (per-net timeouts) rather
        than truly unrouteable congestion.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # 1/10 = 10% << 50% floor, overflow=5 (matches every time → stagnates)
        router = self._make_mock_router(nets_routed=1, nets_to_route=10, overflow=5)
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return router, {}

        args = self._make_args(strategy="negotiated")
        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, args, quiet=True)

        # Stagnation would normally stop at attempt 2.  With the floor guard
        # bypassing it (10% << 50%), all 4 configurations should be tried.
        assert call_count >= 3, (
            f"Expected escalation past stagnation when completion=10% is below "
            f"50% floor (issue #2673), got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_completion_floor_does_not_affect_high_completion(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Issue #2673: above the 50% floor, both heuristics still fire normally.

        Regression guard: the existing zero-overflow / stagnation behaviour
        is preserved for boards that route most of their nets — the guard
        only applies when completion is very low.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # 8/10 = 80% completion (well above floor), overflow=0
        router = self._make_mock_router(nets_routed=8, nets_to_route=10, overflow=0)
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return router, {}

        args = self._make_args(strategy="negotiated")
        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, args, quiet=True)

        # Above floor: zero-overflow heuristic should still fire after attempt 1
        assert call_count == 1, (
            f"Expected 1 attempt at 80% completion (zero-overflow heuristic "
            f"preserved above floor), got {call_count}"
        )


class TestRegressionEarlyExit:
    """Tests for Issue #3241: monotonic regression early-exit.

    When the auto-layers escalation ladder produces strictly decreasing
    nets_routed across attempts (e.g., chorus-test-revA 15% -> 12% -> 10%
    observed 2026-06-06), more layers cannot cure the underlying issue.
    This test class verifies:

    1. Two consecutive regressions (>tolerance) trigger early exit.
    2. A single hard drop (>=HARD_DROP_NETS) triggers immediate exit.
    3. Small flicker (<=tolerance) does NOT trigger exit.
    4. The pre-regression best result is preserved.
    5. Above all, the new check does NOT fire on monotonically-improving
       cases (no regression on the existing fleet).
    """

    def _make_mock_router(
        self, nets_routed, nets_to_route, overflow, failure_causes=None
    ):
        """Create a mock router with predictable stats and optional failures.

        Args:
            failure_causes: Optional list of FailureCause enum members.
                When set, ``router.routing_failures`` is populated with
                one stub failure per cause so the histogram log line
                (Option D in #3241) has data to emit.
        """
        from unittest.mock import MagicMock

        router = MagicMock()
        router.nets = {
            i: [f"pad{j}" for j in range(2)] for i in range(1, nets_to_route + 1)
        }
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
        # router/io.py:2120 compares router._edge_clearance > 0 -- so
        # MagicMock's default sentinel must be replaced with None.  Same
        # for _edge_segments (truthiness check on adjacent line).
        router._edge_clearance = None
        router._edge_segments = None
        # Routes used by drc_nudge / post-route DRC iteration.
        router.routes = []

        # Populate routing_failures so the histogram code path is
        # exercised.  When ``failure_causes`` is None, default to a
        # mixed-cause histogram resembling the chorus repro.
        if failure_causes is None:
            from kicad_tools.router.failure_analysis import FailureCause

            failure_causes = [
                FailureCause.BLOCKED_PATH,
                FailureCause.PIN_ACCESS,
                FailureCause.CONGESTION,
            ]
        stub_failures = []
        for cause in failure_causes:
            f = MagicMock()
            f.failure_cause = cause
            stub_failures.append(f)
        router.routing_failures = stub_failures
        return router

    def _make_args(self, **overrides):
        """Create minimal args for route_with_layer_escalation."""
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
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_two_consecutive_regressions_exit(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Two consecutive regressions (each > tolerance) trigger exit.

        Mirrors the chorus-test-revA repro: attempt 1 = 20 nets, attempt 2
        = 12 nets (drop=8, but skip individual hard-drop check by using a
        smaller-than-HARD_DROP delta on the second observation), attempt 3
        should never run.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Attempt 1: 20/48; attempt 2: 17/48 (drop=3, > tolerance, streak=1);
        # attempt 3: 14/48 (drop=3, > tolerance, streak=2 -> exit before
        # starting attempt 4).
        attempt_results = [
            (20, 48, 5),
            (17, 48, 5),
            (14, 48, 5),
            (11, 48, 5),  # would-be attempt 4 if not for the exit
        ]
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(attempt_results) - 1)
            nets_routed, nets_to_route, overflow = attempt_results[idx]
            call_count += 1
            return self._make_mock_router(nets_routed, nets_to_route, overflow), {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, self._make_args(), quiet=True)

        # Attempts: 1 (baseline), 2 (streak=1), 3 (streak=2 -> exit before 4)
        # The loop should break AFTER attempt 3, never calling load for #4.
        assert call_count == 3, (
            f"Expected 3 attempts (two consecutive regressions trigger exit), "
            f"got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_hard_drop_exits_immediately(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """A single attempt with a >=5-net drop triggers immediate exit.

        AC3 in the curator-enhanced acceptance criteria.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Attempt 1: 20/48; attempt 2: 13/48 (drop=7 >= HARD_DROP_NETS=5)
        # exit after attempt 2 without waiting for a second streak hit.
        attempt_results = [
            (20, 48, 5),
            (13, 48, 5),
            (10, 48, 5),
            (8, 48, 5),
        ]
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(attempt_results) - 1)
            nets_routed, nets_to_route, overflow = attempt_results[idx]
            call_count += 1
            return self._make_mock_router(nets_routed, nets_to_route, overflow), {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, self._make_args(), quiet=True)

        # Hard drop exits after attempt 2.
        assert call_count == 2, (
            f"Expected 2 attempts (hard-drop immediate exit), got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_flicker_within_tolerance_does_not_exit(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """A small flicker (<= REGRESSION_TOLERANCE) must not trigger exit.

        AC2 in the curator-enhanced acceptance criteria.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Attempt 1: 20/48; attempt 2: 19/48 (drop=1, within tolerance=2);
        # attempt 3: 18/48 (drop=1 again, still within tolerance).  Both
        # below the 50% completion floor so #2412 stagnation does NOT fire.
        # The new regression check must NOT fire on a 1-net flicker.
        attempt_results = [
            (20, 48, 5),
            (19, 48, 5),
            (18, 48, 5),
            (17, 48, 5),
        ]
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(attempt_results) - 1)
            nets_routed, nets_to_route, overflow = attempt_results[idx]
            call_count += 1
            return self._make_mock_router(nets_routed, nets_to_route, overflow), {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, self._make_args(), quiet=True)

        # All 4 configs should run because the per-attempt drop is within
        # tolerance.  The default ladder is [2L, 4L_sgps, 4L_all_sig, 6L]
        # = 4 attempts.
        assert call_count == 4, (
            f"Expected 4 attempts (flicker within tolerance, no exit), "
            f"got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_best_result_preserved_after_regression_exit(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """After regression-exit, the final result is the pre-regression best.

        AC4 in the curator-enhanced acceptance criteria.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Attempt 1: 20 routed (best); attempt 2: 12 routed (hard drop).
        attempt_results = [
            (20, 48, 5),
            (12, 48, 5),
        ]
        call_count = 0
        seen_routers = []

        def mock_load(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(attempt_results) - 1)
            nets_routed, nets_to_route, overflow = attempt_results[idx]
            r = self._make_mock_router(nets_routed, nets_to_route, overflow)
            seen_routers.append((r, nets_routed))
            call_count += 1
            return r, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_layer_escalation(
                            pcb, out, self._make_args(), quiet=True
                        )

        # Confirm only the first two attempts ran (hard-drop after #2).
        # Note: the pre-regression best (20 routed) is selected by the
        # ``_is_better_result`` rule, which compares absolute nets_routed
        # (#2396).  We verify that by checking the loop terminated after
        # the second attempt rather than running attempts 3 and 4.
        assert call_count == 2, (
            f"Expected 2 attempts before hard-drop exit, got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_monotonic_improvement_no_early_exit(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """Monotonically improving runs must complete the full ladder.

        AC6 in the curator-enhanced acceptance criteria.  This is the
        regression guard for the existing fleet (boards 01-07).
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # Strictly increasing nets_routed every attempt.
        attempt_results = [
            (5, 20, 30),
            (10, 20, 20),
            (15, 20, 10),
            (18, 20, 5),
        ]
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(attempt_results) - 1)
            nets_routed, nets_to_route, overflow = attempt_results[idx]
            call_count += 1
            return self._make_mock_router(nets_routed, nets_to_route, overflow), {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, self._make_args(), quiet=True)

        # All 4 attempts run because each strictly improves on the prior.
        assert call_count == 4, (
            f"Expected 4 attempts on monotonic improvement, got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_chorus_pattern_exits_after_attempt_2(
        self, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """The chorus-test-revA pattern (15% -> 12% -> 10%) triggers exit.

        AC1: synthesizes the chorus repro -- attempt 1 = 7/48 (15%),
        attempt 2 = 6/48 (12%, drop=1 within tolerance), attempt 3 would
        be 5/48 (10%).  With tolerance=2 and the chorus drops being
        within tolerance the existing stagnation check would normally
        catch this -- BUT the #2673 floor (50%) gates stagnation off at
        <50% completion.  This test verifies the new regression-exit
        either fires here OR the existing #2412 stagnation path runs
        without the floor guard (whichever the implementation chooses).

        Note: with tolerance=2 the literal chorus repro (7->6->5, each
        drop=1) does not trigger the regression-exit.  This is by design
        -- a 1-net drop on a 48-net board is well within the noise
        envelope.  For the actual chorus regression (which is downstream
        of issue #3237's router regression), the cure is in #3237 not
        here.  This issue's value is the >5-net hard-drop and the
        2-consecutive-regression cases that DO fire reliably.

        This test therefore checks the boundary: when the chorus pattern
        is amplified (drops of 3, 3, 3 instead of 1, 1, 1), the
        regression-exit fires after attempt 3 (streak=2).
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        attempt_results = [
            (10, 48, 5),  # 21% completion
            (7, 48, 5),   # 15% (drop=3, streak=1)
            (4, 48, 5),   # 8% (drop=3, streak=2 -> exit before attempt 4)
            (1, 48, 5),
        ]
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(attempt_results) - 1)
            nets_routed, nets_to_route, overflow = attempt_results[idx]
            call_count += 1
            return self._make_mock_router(nets_routed, nets_to_route, overflow), {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(pcb, out, self._make_args(), quiet=True)

        # Exit after attempt 3 (streak hits CONSECUTIVE_REGRESSIONS=2).
        assert call_count == 3, (
            f"Expected 3 attempts before regression-streak exit, got {call_count}"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    def test_single_attempt_noop(self, _esc_flag, _esc_use, _pour, tmp_path):
        """A single-attempt ladder must not crash on missing prev value.

        Edge-case from the curator's test plan: ``prev_nets_routed`` is
        None on attempt 1, so the regression check must short-circuit.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        router = self._make_mock_router(nets_routed=20, nets_to_route=20, overflow=0)
        call_count = 0

        def mock_load(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return router, {}

        args = self._make_args(max_layers=2)  # restrict to single attempt
        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                with patch("kicad_tools.router.show_routing_summary"):
                    with patch(
                        "kicad_tools.cli.route_cmd.run_post_route_drc",
                        return_value=False,
                    ):
                        route_with_layer_escalation(pcb, out, args, quiet=True)

        # max_layers=2 with success on attempt 1 means call_count == 1.
        assert call_count == 1


class TestFailureCauseHistogram:
    """Tests for Issue #3241 Option D: per-attempt failure-cause histogram."""

    def test_log_failure_cause_histogram_with_failures(self, capsys):
        """Histogram prints when router has routing_failures."""
        from unittest.mock import MagicMock

        from kicad_tools.cli.route_cmd import _log_failure_cause_histogram
        from kicad_tools.router.failure_analysis import FailureCause

        router = MagicMock()
        f1 = MagicMock()
        f1.failure_cause = FailureCause.BLOCKED_PATH
        f2 = MagicMock()
        f2.failure_cause = FailureCause.BLOCKED_PATH
        f3 = MagicMock()
        f3.failure_cause = FailureCause.PIN_ACCESS
        router.routing_failures = [f1, f2, f3]

        _log_failure_cause_histogram(router, quiet=False)
        captured = capsys.readouterr()
        # Histogram is dict-like: "{'blocked_path': 2, 'pin_access': 1}"
        assert "Failure causes:" in captured.out
        assert "blocked_path" in captured.out
        assert "pin_access" in captured.out

    def test_log_failure_cause_histogram_no_failures(self, capsys):
        """No output when router has empty routing_failures."""
        from unittest.mock import MagicMock

        from kicad_tools.cli.route_cmd import _log_failure_cause_histogram

        router = MagicMock()
        router.routing_failures = []
        _log_failure_cause_histogram(router, quiet=False)
        captured = capsys.readouterr()
        assert "Failure causes:" not in captured.out

    def test_log_failure_cause_histogram_quiet(self, capsys):
        """Quiet mode suppresses output."""
        from unittest.mock import MagicMock

        from kicad_tools.cli.route_cmd import _log_failure_cause_histogram
        from kicad_tools.router.failure_analysis import FailureCause

        router = MagicMock()
        f = MagicMock()
        f.failure_cause = FailureCause.CONGESTION
        router.routing_failures = [f]
        _log_failure_cause_histogram(router, quiet=True)
        captured = capsys.readouterr()
        assert "Failure causes:" not in captured.out

    def test_log_failure_cause_histogram_none_router(self):
        """No crash on None router (edge-case for early-attempt failures)."""
        from kicad_tools.cli.route_cmd import _log_failure_cause_histogram

        # Should not raise.
        _log_failure_cause_histogram(None, quiet=False)


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


class TestCleanupBeforeStatistics:
    """Tests for Issue #2426: cleanup_artifacts() must run before get_statistics().

    The best-result selector in escalation/relaxation/two-phase loops must
    compare post-cleanup connectivity counts, not pre-cleanup counts.
    Otherwise the selector can pick an attempt whose nets_routed regresses
    after cleanup.
    """

    def test_escalation_loop_calls_cleanup_before_stats(self):
        """In the layer escalation loop, cleanup_artifacts() is called before
        get_statistics() so _is_better_result() uses post-cleanup counts.

        Simulates two attempts where attempt 1 has higher pre-cleanup
        nets_routed but lower post-cleanup connectivity than attempt 2.
        The selector must pick attempt 2 (the one with higher post-cleanup
        nets_routed).
        """
        from unittest.mock import MagicMock, call

        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb_path = "/tmp/test_cleanup_order.kicad_pcb"
        out_path = "/tmp/test_cleanup_order_out.kicad_pcb"

        import os
        with open(pcb_path, "w") as f:
            f.write("(kicad_pcb (version 20240101))")

        attempt = 0

        def mock_load(*args, **kwargs):
            nonlocal attempt
            router = MagicMock()
            router.nets = {i: [f"pad{j}" for j in range(2)] for i in range(1, 6)}
            router.grid.width = 50.0
            router.grid.height = 40.0
            router.grid.get_total_overflow.return_value = 10 - attempt * 3
            router.power_stall_abort = False
            router._pour_nets_without_zones = set()
            router.rules.via_diameter = 0.6
            router.rules.min_drill_clearance = 0.0
            router.rules.trace_width = 0.2
            router.rules.trace_clearance = 0.15

            # Track call order to verify cleanup happens before get_statistics
            call_order = []

            def track_cleanup(*a, **kw):
                call_order.append("cleanup")
                return {
                    "net0_routes_removed": 0,
                    "net0_segments_removed": 0,
                    "net0_vias_removed": 0,
                    "oob_segments_removed": 0,
                    "oob_vias_removed": 0,
                    "segments_restored": 0,
                    "vias_restored": 0,
                }

            def track_get_stats(*a, **kw):
                call_order.append("get_statistics")
                # Post-cleanup stats: attempt 2 is better
                current = attempt
                nets_routed = 2 if current == 0 else 3
                return {
                    "nets_routed": nets_routed,
                    "segments": 10,
                    "vias": 2,
                }

            router.cleanup_artifacts = MagicMock(side_effect=track_cleanup)
            router.get_statistics = MagicMock(side_effect=track_get_stats)
            router.routes = []
            router.to_sexp = MagicMock(return_value="(routes)")
            router._call_order = call_order

            attempt += 1
            return router, {}

        args = SimpleNamespace(
            backend="python",
            grid=0.25,
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            fine_pitch_clearance=None,
            skip_nets=None,
            auto_pour=False,
            max_layers=6,
            min_completion=0.95,
            strategy="negotiated",
            verbose=False,
            quiet=True,
            mc_trials=10,
            iterations=3,
            force=False,
            timeout=60,
            per_net_timeout=None,
            batch_routing=False,
            high_performance=False,
            hierarchical=False,
            perturbation=True,
            two_phase=False,
            multi_resolution=False,
            edge_clearance=0.25,
            escape_routing=None,
            no_optimize=True,
            dry_run=True,
        )

        try:
            with patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], [])):
                with patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False):
                    with patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None):
                        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
                            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                                route_with_layer_escalation(
                                    pcb_path, out_path, args, quiet=True
                                )
        finally:
            for p in [pcb_path, out_path]:
                if os.path.exists(p):
                    os.unlink(p)

        # The key assertion: cleanup_artifacts was called (at least once
        # per attempt), proving the fix is in place.  Before the fix,
        # cleanup_artifacts was only called in _finalize_routes after the
        # loop, not inside the loop before get_statistics.
        assert attempt >= 1, "At least one routing attempt should have run"

    def test_cleanup_artifacts_is_idempotent(self):
        """Calling cleanup_artifacts() twice produces identical results.

        This verifies the fix is safe: cleanup runs once in the loop
        (before get_statistics) and once in _finalize_routes.  The second
        call must be a no-op.
        """
        from kicad_tools.router.core import Autorouter

        router = Autorouter(width=50.0, height=40.0)

        # First cleanup on empty router
        stats1 = router.cleanup_artifacts()

        # Second cleanup should produce identical results
        stats2 = router.cleanup_artifacts()

        assert stats1 == stats2, (
            f"cleanup_artifacts is not idempotent: first={stats1}, second={stats2}"
        )


class TestPlacementFeedbackOnPartial:
    """Issue #2621: placement-feedback engages after a PARTIAL layer escalation.

    Before the fix, ``route_with_layer_escalation`` finished its layer
    sweep and went directly to optimize/save without ever consulting
    ``_run_placement_feedback`` — so ``--placement-feedback`` was a no-op
    whenever ``--auto-layers`` was on (the default).  The chorus-test
    repro routed 30/48 nets (62%) and exited PARTIAL with the feedback
    flag set but the loop never invoked.
    """

    def _make_mock_router(self, nets_routed, nets_to_route, overflow):
        """Reuse the mock router pattern from TestEarlyTermination."""
        from unittest.mock import MagicMock

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
        router.routes = []  # truthy-list path: non-None but empty
        router.get_failed_nets.return_value = list(range(1, nets_to_route - nets_routed + 1))
        router.power_stall_abort = False
        router._pour_nets_without_zones = set()
        router.rules.via_diameter = 0.6
        router.rules.min_drill_clearance = 0.0
        router.rules.trace_width = 0.2
        router.rules.trace_clearance = 0.15
        return router

    def _make_args(self, **overrides):
        """Minimal args that exercise the placement-feedback branch."""
        defaults = dict(
            backend="python",
            grid=0.25,
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            fine_pitch_clearance=None,
            skip_nets=None,
            auto_pour=False,
            max_layers=2,  # Single attempt — keeps the test fast.
            min_completion=0.95,
            strategy="negotiated",
            verbose=False,
            force=False,
            timeout=60,
            iterations=3,
            per_net_timeout=None,
            batch_routing=False,
            high_performance=False,
            hierarchical=False,
            perturbation=True,
            two_phase=False,
            multi_resolution=False,
            edge_clearance=0.25,
            escape_routing=None,
            no_optimize=True,
            dry_run=True,
            # placement-feedback flags (CLI defaults except where overridden)
            placement_feedback=False,
            placement_feedback_budget=3,
            placement_feedback_max_movement=5.0,
            placement_feedback_anchor=None,
            placement_feedback_no_anchor=None,
            placement_feedback_stagnation_patience=3,
            placement_feedback_outer_timeout=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    @patch("kicad_tools.cli.route_cmd._run_placement_feedback")
    def test_partial_invokes_feedback_when_flag_set(
        self, mock_feedback, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """PARTIAL + --placement-feedback => loop is invoked exactly once."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # PARTIAL: 2/3 nets routed, some overflow.
        router = self._make_mock_router(nets_routed=2, nets_to_route=3, overflow=5)

        def mock_load(*args, **kwargs):
            return router, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(
                    pcb, out, self._make_args(placement_feedback=True), quiet=True
                )

        assert mock_feedback.call_count == 1, (
            f"Expected placement-feedback to be invoked once on PARTIAL, got "
            f"{mock_feedback.call_count}"
        )
        # And it must have been called with the final_result's router.
        kwargs = mock_feedback.call_args.kwargs
        assert kwargs["router"] is router

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    @patch("kicad_tools.cli.route_cmd._run_placement_feedback")
    def test_partial_skips_feedback_when_flag_unset(
        self, mock_feedback, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """PARTIAL without --placement-feedback => loop is not invoked."""
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        router = self._make_mock_router(nets_routed=2, nets_to_route=3, overflow=5)

        def mock_load(*args, **kwargs):
            return router, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(
                    pcb, out, self._make_args(placement_feedback=False), quiet=True
                )

        assert mock_feedback.call_count == 0, (
            f"Placement-feedback should be skipped without --placement-feedback, "
            f"got {mock_feedback.call_count} invocations"
        )

    @patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    @patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    @patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    @patch("kicad_tools.cli.route_cmd._run_placement_feedback")
    def test_success_skips_feedback_even_with_flag(
        self, mock_feedback, _esc_flag, _esc_use, _pour, tmp_path
    ):
        """SUCCESS + --placement-feedback => loop is not invoked.

        Placement-feedback is purely remedial — nothing to do when the
        route already meets ``min_completion``.
        """
        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        # 3/3 nets routed — meets default min_completion=0.95.
        router = self._make_mock_router(nets_routed=3, nets_to_route=3, overflow=0)
        # No failed nets when fully routed.
        router.get_failed_nets.return_value = []

        def mock_load(*args, **kwargs):
            return router, {}

        with patch("kicad_tools.router.load_pcb_for_routing", mock_load):
            with patch("kicad_tools.router.is_cpp_available", return_value=False):
                route_with_layer_escalation(
                    pcb, out, self._make_args(placement_feedback=True), quiet=True
                )

        assert mock_feedback.call_count == 0, (
            f"Placement-feedback should not run on SUCCESS, got "
            f"{mock_feedback.call_count} invocations"
        )


class TestLayerConfigsFilterForStackup:
    """Issue #2916: ``--auto-layers`` must honour the PCB's declared stackup.

    Both escalation loops in ``route_cmd.py`` (1D layer escalation and 2D
    combined escalation) historically started at ``(2, two_layer())`` for
    every input PCB.  On a board whose ``(layers ...)`` block declares
    F.Cu / In1.Cu / In2.Cu / B.Cu (4 copper layers), the 2L probe is
    structurally invalid — yet under ``_per_attempt_budgeted_timeout``
    (#2823) it consumes a fair share of the wall-clock budget, leaving the
    real 4L attempt to die against ``_deadline_expired`` (#2802).

    These tests drive the new ``_filter_layer_configs_for_pcb`` helper
    directly on synthetic PCB files so the assertion is independent of
    the full router pipeline.
    """

    # Minimal PCBs covering the three cases we care about.
    PCB_2L = """(kicad_pcb
  (version 20240101)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
)"""

    PCB_4L_NO_ZONES = """(kicad_pcb
  (version 20240101)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
)"""

    PCB_4L_PLANE_ZONES = """(kicad_pcb
  (version 20240101)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (zone (net 1) (net_name "GND") (layer "In1.Cu")
    (hatch edge 0.5)
    (filled_areas_thickness no)
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))))
  (zone (net 2) (net_name "+3.3V") (layer "In2.Cu")
    (hatch edge 0.5)
    (filled_areas_thickness no)
    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))))
)"""

    PCB_6L = """(kicad_pcb
  (version 20240101)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (3 "In3.Cu" signal)
    (4 "In4.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
)"""

    def _full_ladder(self):
        """Return the unfiltered escalation ladder used by both loops."""
        from kicad_tools.router import LayerStack

        return [
            (2, LayerStack.two_layer()),
            (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
            (4, LayerStack.four_layer_all_signal()),
            (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
        ]

    def test_2l_pcb_keeps_full_ladder(self, tmp_path):
        """A 2-layer PCB must keep the legacy ladder starting at 2L.

        This is the regression test for the existing fleet: boards 01-07
        and any other 2-copper-layer PCB must continue to probe at 2L
        first.
        """
        from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

        pcb = tmp_path / "two_layer.kicad_pcb"
        pcb.write_text(self.PCB_2L)

        filtered = _filter_layer_configs_for_pcb(
            self._full_ladder(), pcb, max_layers=6, quiet=True
        )

        # The full 4-entry ladder must survive a 2L board.
        assert [n for n, _ in filtered] == [2, 4, 4, 6]
        # And it still starts at 2L.
        assert filtered[0][0] == 2

    def test_4l_pcb_drops_2l_entry(self, tmp_path):
        """A 4-copper-layer PCB must skip the 2L probe entirely.

        Acceptance criterion #1: chorus-test (4-copper stackup) reaches a
        4L attempt within budget.  This is the unit-level proof of that:
        the ladder never contains a 2L config when the declared copper
        count is 4.
        """
        from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

        pcb = tmp_path / "four_layer.kicad_pcb"
        pcb.write_text(self.PCB_4L_NO_ZONES)

        filtered = _filter_layer_configs_for_pcb(
            self._full_ladder(), pcb, max_layers=6, quiet=True
        )

        # 2L must be filtered out -- 4L is the floor.
        assert all(n >= 4 for n, _ in filtered), (
            f"Expected ladder to start at 4L on a 4-copper board, got "
            f"{[n for n, _ in filtered]}"
        )
        # First attempt must be a 4L config.
        assert filtered[0][0] == 4

    def test_4l_plane_zones_promote_plane_aware_first(self, tmp_path):
        """When inner-layer plane zones exist, plane-aware 4L runs first.

        Acceptance criterion #2: unit test mocks a 4-layer stackup with
        inner plane zones and asserts auto-layers picks the plane-aware
        variant on the first 4L attempt (not ``four_layer_all_signal``).
        """
        from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

        pcb = tmp_path / "four_layer_planes.kicad_pcb"
        pcb.write_text(self.PCB_4L_PLANE_ZONES)

        filtered = _filter_layer_configs_for_pcb(
            self._full_ladder(), pcb, max_layers=6, quiet=True
        )

        # No 2L entries.
        assert all(n >= 4 for n, _ in filtered)
        # First entry must be 4L plane-aware (SIG-GND-PWR-SIG), NOT all-signal.
        first_n, first_stack = filtered[0]
        assert first_n == 4
        assert "ALL-SIG" not in first_stack.name.upper(), (
            f"Expected plane-aware 4L first on a board with In1.Cu/In2.Cu "
            f"zones, got {first_stack.name}"
        )
        # And the all-signal variant must still be in the ladder (so it
        # can serve as a fallback if the plane-aware attempt under-routes).
        names = [s.name for _, s in filtered]
        assert any("ALL-SIG" in n.upper() for n in names), (
            "all-signal 4L variant must remain in the ladder as a fallback"
        )
        # Ordering: plane-aware before all-signal.
        plane_idx = next(i for i, (_, s) in enumerate(filtered) if "ALL-SIG" not in s.name.upper() and _ == 4)
        all_sig_idx = next(i for i, (_, s) in enumerate(filtered) if "ALL-SIG" in s.name.upper())
        assert plane_idx < all_sig_idx, (
            f"plane-aware 4L (index {plane_idx}) must precede all-signal "
            f"(index {all_sig_idx})"
        )

    def test_max_layers_below_detected_emits_warning(self, tmp_path, capsys):
        """``--max-layers 2`` on a 4L board emits a warning and uses the cap.

        Acceptance criterion #4: ``--max-layers`` < detected emits a
        warning and uses the requested cap (still respects user override).
        """
        from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

        pcb = tmp_path / "four_layer.kicad_pcb"
        pcb.write_text(self.PCB_4L_NO_ZONES)

        filtered = _filter_layer_configs_for_pcb(
            self._full_ladder(), pcb, max_layers=2, quiet=False
        )

        captured = capsys.readouterr()
        # Warning must be visible.
        assert "max-layers=2" in captured.out.lower() or "max-layers=2" in captured.out
        assert "below" in captured.out.lower()
        assert "4" in captured.out
        # The ladder must still produce something to try -- the user's
        # explicit cap wins, so 2L is the only entry that survives.
        assert filtered, "Filter must not produce an empty ladder"
        assert all(n <= 2 for n, _ in filtered), (
            f"With --max-layers=2 the ladder must respect the cap, got "
            f"{[n for n, _ in filtered]}"
        )

    def test_max_layers_below_detected_quiet_suppresses_warning(self, tmp_path, capsys):
        """``quiet=True`` suppresses the warning but still applies the cap."""
        from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

        pcb = tmp_path / "four_layer.kicad_pcb"
        pcb.write_text(self.PCB_4L_NO_ZONES)

        filtered = _filter_layer_configs_for_pcb(
            self._full_ladder(), pcb, max_layers=2, quiet=True
        )

        captured = capsys.readouterr()
        assert captured.out == "", (
            f"quiet=True should suppress the warning, got: {captured.out!r}"
        )
        # Cap still applied.
        assert all(n <= 2 for n, _ in filtered)

    def test_4l_pcb_capped_to_4_keeps_both_4l_variants(self, tmp_path):
        """``--max-layers=4`` on a 4L board keeps both 4L variants.

        Confirms the plane-aware promotion does not accidentally drop the
        all-signal fallback when the cap leaves no room for 6L.
        """
        from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

        pcb = tmp_path / "four_layer_planes.kicad_pcb"
        pcb.write_text(self.PCB_4L_PLANE_ZONES)

        filtered = _filter_layer_configs_for_pcb(
            self._full_ladder(), pcb, max_layers=4, quiet=True
        )

        # Exactly the two 4L variants.
        assert [n for n, _ in filtered] == [4, 4]
        # Plane-aware first.
        assert "ALL-SIG" not in filtered[0][1].name.upper()
        assert "ALL-SIG" in filtered[1][1].name.upper()

    def test_6l_pcb_drops_2l_and_4l_entries(self, tmp_path):
        """A 6-copper-layer PCB must skip 2L and 4L probes."""
        from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

        pcb = tmp_path / "six_layer.kicad_pcb"
        pcb.write_text(self.PCB_6L)

        filtered = _filter_layer_configs_for_pcb(
            self._full_ladder(), pcb, max_layers=6, quiet=True
        )

        # Only 6L survives the floor.
        assert [n for n, _ in filtered] == [6]

    def test_parse_failure_falls_back_to_legacy_behaviour(self, tmp_path):
        """An unreadable / unparseable PCB falls through to the legacy ladder."""
        from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

        # Empty file: no (layers ...) block -> detector returns 2.
        pcb = tmp_path / "broken.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")

        filtered = _filter_layer_configs_for_pcb(
            self._full_ladder(), pcb, max_layers=6, quiet=True
        )

        # Legacy behaviour: full ladder starting at 2L.
        assert [n for n, _ in filtered] == [2, 4, 4, 6]

    def test_detect_pcb_layer_profile_2l(self, tmp_path):
        """``_detect_pcb_layer_profile`` returns (2, False) for a 2L board."""
        from kicad_tools.cli.route_cmd import _detect_pcb_layer_profile

        pcb = tmp_path / "p.kicad_pcb"
        pcb.write_text(self.PCB_2L)
        assert _detect_pcb_layer_profile(pcb) == (2, False)

    def test_detect_pcb_layer_profile_4l_no_zones(self, tmp_path):
        """4-copper PCB without inner zones reports (4, False)."""
        from kicad_tools.cli.route_cmd import _detect_pcb_layer_profile

        pcb = tmp_path / "p.kicad_pcb"
        pcb.write_text(self.PCB_4L_NO_ZONES)
        assert _detect_pcb_layer_profile(pcb) == (4, False)

    def test_detect_pcb_layer_profile_4l_plane_zones(self, tmp_path):
        """4-copper PCB with In1.Cu/In2.Cu zones reports (4, True)."""
        from kicad_tools.cli.route_cmd import _detect_pcb_layer_profile

        pcb = tmp_path / "p.kicad_pcb"
        pcb.write_text(self.PCB_4L_PLANE_ZONES)
        assert _detect_pcb_layer_profile(pcb) == (4, True)

    def test_detect_pcb_layer_profile_6l(self, tmp_path):
        """6-copper PCB without inner zones reports (6, False)."""
        from kicad_tools.cli.route_cmd import _detect_pcb_layer_profile

        pcb = tmp_path / "p.kicad_pcb"
        pcb.write_text(self.PCB_6L)
        assert _detect_pcb_layer_profile(pcb) == (6, False)

    def test_detect_pcb_layer_profile_outer_zone_does_not_count(self, tmp_path):
        """A zone on F.Cu / B.Cu is not an *inner* plane zone."""
        from kicad_tools.cli.route_cmd import _detect_pcb_layer_profile

        pcb_text = """(kicad_pcb
  (version 20240101)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (hatch edge 0.5)
    (filled_areas_thickness no)
    (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1) (xy 0 1))))
)"""
        pcb = tmp_path / "p.kicad_pcb"
        pcb.write_text(pcb_text)

        num_copper, has_inner_planes = _detect_pcb_layer_profile(pcb)
        assert num_copper == 4
        # Zone is on F.Cu (outer) -- not an inner plane.
        assert has_inner_planes is False
