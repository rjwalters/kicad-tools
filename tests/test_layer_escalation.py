"""Tests for automatic layer escalation feature (issue #869).

Verifies that the --auto-layers flag correctly escalates layer count
when routing fails to achieve the minimum completion threshold.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestLayerEscalationCLIParameters:
    """Tests for --auto-layers parameter handling in route command."""

    def test_auto_layers_parameter_passed_when_set(self):
        """auto-layers parameter is passed when specified."""
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
            min_completion=0.95,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--auto-layers" in call_args

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
