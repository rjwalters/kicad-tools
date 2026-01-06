"""Tests for route command parameter handling (issue #418).

Verifies that CLI parameters like --grid and --clearance are correctly
passed through the command handler to the underlying route_cmd.main().
"""

from types import SimpleNamespace
from unittest.mock import patch


class TestRouteCommandGridParameter:
    """Tests for --grid parameter handling in route command."""

    def test_grid_parameter_passed_when_not_default(self):
        """Grid parameter is passed when different from default 0.25."""
        from kicad_tools.cli.commands.routing import run_route_command

        # Create args simulating --grid 0.1 (non-default)
        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid=0.1,  # Non-default value
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
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            # Verify --grid 0.1 is in the arguments
            call_args = mock_main.call_args[0][0]
            assert "--grid" in call_args
            grid_idx = call_args.index("--grid")
            assert call_args[grid_idx + 1] == "0.1"

    def test_grid_parameter_not_duplicated_when_default(self):
        """Grid parameter is not passed when equal to default 0.25."""
        from kicad_tools.cli.commands.routing import run_route_command

        # Create args simulating default --grid 0.25
        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid=0.25,  # Default value - should not be passed
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
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            # Verify --grid is NOT in the arguments (uses default)
            call_args = mock_main.call_args[0][0]
            assert "--grid" not in call_args


class TestRouteCommandClearanceParameter:
    """Tests for --clearance parameter handling in route command."""

    def test_clearance_parameter_passed_when_not_default(self):
        """Clearance parameter is passed when different from default 0.15."""
        from kicad_tools.cli.commands.routing import run_route_command

        # Create args simulating --clearance 0.127 (non-default)
        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid=0.25,
            trace_width=0.2,
            clearance=0.127,  # Non-default value
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            # Verify --clearance 0.127 is in the arguments
            call_args = mock_main.call_args[0][0]
            assert "--clearance" in call_args
            clearance_idx = call_args.index("--clearance")
            assert call_args[clearance_idx + 1] == "0.127"

    def test_clearance_parameter_not_duplicated_when_default(self):
        """Clearance parameter is not passed when equal to default 0.15."""
        from kicad_tools.cli.commands.routing import run_route_command

        # Create args simulating default --clearance 0.15
        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid=0.25,
            trace_width=0.2,
            clearance=0.15,  # Default value - should not be passed
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            # Verify --clearance is NOT in the arguments (uses default)
            call_args = mock_main.call_args[0][0]
            assert "--clearance" not in call_args


class TestRouteCommandDefaultConsistency:
    """Tests to verify default values are consistent across modules."""

    def test_parser_defaults_match_handler_checks(self):
        """Verify parser defaults match the values checked in routing.py handler."""

        from kicad_tools.cli.parser import create_parser

        parser = create_parser()

        # Extract route subparser defaults
        # We need to parse with empty args to get defaults
        args = parser.parse_args(["route", "test.kicad_pcb", "--dry-run"])

        # These should match the checks in routing.py
        assert args.grid == 0.25, "Grid default should be 0.25"
        assert args.clearance == 0.15, "Clearance default should be 0.15"
        assert args.trace_width == 0.2, "Trace width default should be 0.2"
        assert args.via_drill == 0.3, "Via drill default should be 0.3"
        assert args.via_diameter == 0.6, "Via diameter default should be 0.6"

    def test_route_cmd_defaults_match_parser_defaults(self):
        """Verify route_cmd.py defaults match parser.py defaults."""
        import contextlib
        import sys
        from io import StringIO
        from unittest.mock import patch

        from kicad_tools.cli.route_cmd import main as route_main

        # Parse with just the PCB file to get defaults
        # We can't easily test this without mocking, so we verify
        # the argparse setup in route_cmd matches what we expect
        # by checking the help text contains the right defaults
        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()

        # Verify the defaults in help text
        assert "default: 0.25" in help_text, "route_cmd --grid default should be 0.25"
        assert "default: 0.15" in help_text, "route_cmd --clearance default should be 0.15"


class TestRouteCommandGridClearanceValidation:
    """Tests for grid vs clearance validation (issue #529).

    When grid resolution exceeds clearance, the router will create DRC violations.
    The route command should fail unless --force is specified.
    """

    def test_grid_exceeds_clearance_fails_without_force(self, tmp_path):
        """Route command fails when grid > clearance without --force."""
        from kicad_tools.cli.route_cmd import main as route_main

        # Create a minimal test PCB file
        pcb_content = """(kicad_pcb (version 20240101) (generator "test"))"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # grid=0.25, clearance=0.127 → grid > clearance → should fail
        result = route_main(
            [
                str(test_pcb),
                "--grid",
                "0.25",
                "--clearance",
                "0.127",
                "--dry-run",
                "--quiet",
            ]
        )

        assert result == 1, "Should return 1 (error) when grid > clearance"

    def test_grid_exceeds_clearance_succeeds_with_force(self, tmp_path):
        """Route command continues when grid > clearance with --force."""
        from kicad_tools.cli.route_cmd import main as route_main

        # Create a minimal test PCB file
        pcb_content = """(kicad_pcb (version 20240101) (generator "test"))"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # grid=0.25, clearance=0.127 with --force → should continue
        # Note: May still fail for other reasons (empty PCB), but should pass validation
        result = route_main(
            [
                str(test_pcb),
                "--grid",
                "0.25",
                "--clearance",
                "0.127",
                "--force",
                "--dry-run",
                "--quiet",
            ]
        )

        # With --force, it should pass the validation step.
        # It may fail later due to empty PCB, but error code won't be 1
        # (validation error). We check it gets past validation.
        # A minimal PCB with no nets will return 0 with dry-run
        assert result != 1 or result == 0, "Should pass grid/clearance validation with --force"

    def test_grid_within_clearance_succeeds_without_force(self, tmp_path):
        """Route command succeeds when grid <= clearance without --force."""
        from kicad_tools.cli.route_cmd import main as route_main

        # Create a minimal test PCB file
        pcb_content = """(kicad_pcb (version 20240101) (generator "test"))"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # grid=0.1, clearance=0.15 → grid < clearance → should succeed
        result = route_main(
            [
                str(test_pcb),
                "--grid",
                "0.1",
                "--clearance",
                "0.15",
                "--dry-run",
                "--quiet",
            ]
        )

        # Should not fail due to grid/clearance validation
        # (may have other issues with minimal PCB, but won't be validation error)
        assert result == 0, "Should succeed when grid <= clearance"

    def test_force_flag_in_help(self):
        """Verify --force flag is documented in help text."""
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

        assert "--force" in help_text, "Help text should document --force flag"
        assert "clearance" in help_text.lower(), "Help text should mention clearance"
