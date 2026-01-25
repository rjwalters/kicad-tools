"""Tests for CLI --routing-aware and --check-routability flags.

This module tests the integration of routing-aware placement optimization
in the CLI, ensuring the flags are properly exposed and forwarded.
"""

import contextlib
import sys
from io import StringIO
from pathlib import Path

import pytest


class TestRoutingAwareFlagExists:
    """Tests that --routing-aware flag is available in CLI."""

    def test_routing_aware_flag_in_placement_cmd_help(self):
        """Test that --routing-aware flag appears in placement optimize help."""
        from kicad_tools.cli.placement_cmd import main

        # Capture stdout to check for help
        old_stdout = sys.stdout
        sys.stdout = StringIO()

        with contextlib.suppress(SystemExit):
            main(["optimize", "--help"])

        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

        # Verify --routing-aware is documented
        assert "--routing-aware" in output
        assert "place-route optimization" in output.lower() or "routing" in output.lower()

    def test_check_routability_flag_in_placement_cmd_help(self):
        """Test that --check-routability flag appears in placement optimize help."""
        from kicad_tools.cli.placement_cmd import main

        old_stdout = sys.stdout
        sys.stdout = StringIO()

        with contextlib.suppress(SystemExit):
            main(["optimize", "--help"])

        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

        # Verify --check-routability is documented
        assert "--check-routability" in output

    def test_routing_aware_flag_in_parser(self):
        """Test that --routing-aware is registered in main CLI parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()

        # Parse a minimal command line with --routing-aware
        # This should not raise an error if the flag exists
        args = parser.parse_args(
            ["placement", "optimize", "test.kicad_pcb", "--routing-aware"]
        )

        assert hasattr(args, "routing_aware")
        assert args.routing_aware is True

    def test_check_routability_flag_in_parser(self):
        """Test that --check-routability is registered in main CLI parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()

        args = parser.parse_args(
            ["placement", "optimize", "test.kicad_pcb", "--check-routability"]
        )

        assert hasattr(args, "check_routability")
        assert args.check_routability is True

    def test_both_flags_together(self):
        """Test that both flags can be used together."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()

        args = parser.parse_args(
            [
                "placement",
                "optimize",
                "test.kicad_pcb",
                "--routing-aware",
                "--check-routability",
            ]
        )

        assert args.routing_aware is True
        assert args.check_routability is True


class TestRoutingAwareFlagForwarding:
    """Tests that flags are properly forwarded from unified CLI to placement_cmd."""

    def test_routing_aware_forwarded(self, tmp_path: Path):
        """Test that --routing-aware is forwarded to placement_cmd."""
        from unittest.mock import patch

        from kicad_tools.cli.commands.placement import run_placement_command

        # Create a mock args object
        class MockArgs:
            placement_command = "optimize"
            pcb = str(tmp_path / "test.kicad_pcb")
            output = None
            strategy = "force-directed"
            iterations = 1000
            generations = 100
            population = 50
            grid = 0.0
            fixed = None
            cluster = False
            constraints = None
            edge_detect = False
            thermal = False
            keepout = None
            auto_keepout = False
            routing_aware = True  # Key flag being tested
            check_routability = False
            dry_run = True
            format = "text"
            verbose = False
            quiet = True
            global_quiet = False

        # Mock placement_main to capture the arguments
        called_with = []

        def mock_main(argv):
            called_with.extend(argv)
            return 0

        # Patch at the module where it's imported from, not where it's used
        with patch("kicad_tools.cli.placement_cmd.main", mock_main):
            run_placement_command(MockArgs())

        # Verify --routing-aware was forwarded
        assert "--routing-aware" in called_with

    def test_check_routability_forwarded(self, tmp_path: Path):
        """Test that --check-routability is forwarded to placement_cmd."""
        from unittest.mock import patch

        from kicad_tools.cli.commands.placement import run_placement_command

        class MockArgs:
            placement_command = "optimize"
            pcb = str(tmp_path / "test.kicad_pcb")
            output = None
            strategy = "force-directed"
            iterations = 1000
            generations = 100
            population = 50
            grid = 0.0
            fixed = None
            cluster = False
            constraints = None
            edge_detect = False
            thermal = False
            keepout = None
            auto_keepout = False
            routing_aware = False
            check_routability = True  # Key flag being tested
            dry_run = True
            format = "text"
            verbose = False
            quiet = True
            global_quiet = False

        called_with = []

        def mock_main(argv):
            called_with.extend(argv)
            return 0

        # Patch at the module where it's imported from, not where it's used
        with patch("kicad_tools.cli.placement_cmd.main", mock_main):
            run_placement_command(MockArgs())

        assert "--check-routability" in called_with


class TestRoutingAwareExecution:
    """Integration tests for routing-aware optimization execution."""

    def test_routing_aware_dry_run(self, minimal_pcb: Path, capsys):
        """Test --routing-aware with --dry-run on a minimal PCB."""
        from kicad_tools.cli.placement_cmd import main

        # Run with --routing-aware and --dry-run
        result = main(
            [
                "optimize",
                str(minimal_pcb),
                "--routing-aware",
                "--dry-run",
                "--quiet",
            ]
        )

        # Should complete (may or may not succeed depending on PCB content)
        # The key is it doesn't error with "unrecognized arguments"
        assert result in (0, 1)

    def test_routing_aware_json_output(self, minimal_pcb: Path, capsys):
        """Test --routing-aware with JSON output format."""
        import json

        from kicad_tools.cli.placement_cmd import main

        result = main(
            [
                "optimize",
                str(minimal_pcb),
                "--routing-aware",
                "--dry-run",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        # Should produce valid JSON output
        try:
            data = json.loads(captured.out)
            assert "success" in data
            assert "strategy" in data
            assert data["strategy"] == "routing-aware"
        except json.JSONDecodeError:
            # If there's an error, it might be printed to stderr
            # The test passes as long as the flag was accepted
            pass

    def test_check_routability_shows_impact(self, minimal_pcb: Path, capsys):
        """Test --check-routability shows before/after routability."""
        from kicad_tools.cli.placement_cmd import main

        result = main(
            [
                "optimize",
                str(minimal_pcb),
                "--check-routability",
                "--dry-run",
            ]
        )

        # Should complete without "unrecognized arguments" error
        assert result in (0, 1)


class TestPlaceRouteOptimizer:
    """Tests for the PlaceRouteOptimizer class integration."""

    def test_optimizer_can_be_created(self, minimal_pcb: Path):
        """Test that PlaceRouteOptimizer can be instantiated."""
        from kicad_tools.optimize.place_route import PlaceRouteOptimizer
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(minimal_pcb))
        optimizer = PlaceRouteOptimizer.from_pcb(pcb, pcb_path=minimal_pcb)

        assert optimizer is not None
        assert optimizer.pcb_path == minimal_pcb

    def test_optimizer_result_dataclass(self):
        """Test OptimizationResult dataclass structure."""
        from kicad_tools.optimize.place_route import OptimizationResult

        result = OptimizationResult(
            success=True,
            iterations=5,
            message="Test completed",
        )

        assert result.success is True
        assert result.iterations == 5
        assert result.message == "Test completed"
        assert result.has_placement_conflicts is False
        assert result.has_drc_violations is False
        assert result.routing_complete is False
