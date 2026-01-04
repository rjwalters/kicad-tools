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
