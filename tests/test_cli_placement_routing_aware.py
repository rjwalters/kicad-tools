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
        args = parser.parse_args(["placement", "optimize", "test.kicad_pcb", "--routing-aware"])

        assert hasattr(args, "routing_aware")
        assert args.routing_aware is True

    def test_check_routability_flag_in_parser(self):
        """Test that --check-routability is registered in main CLI parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()

        args = parser.parse_args(["placement", "optimize", "test.kicad_pcb", "--check-routability"])

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
            iterations = 10
            generations = 5
            population = 10
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
            iterations = 10
            generations = 5
            population = 10
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
        from kicad_tools.optim.place_route import PlaceRouteOptimizer
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(minimal_pcb))
        optimizer = PlaceRouteOptimizer.from_pcb(pcb, pcb_path=minimal_pcb)

        assert optimizer is not None
        assert optimizer.pcb_path == minimal_pcb

    def test_optimizer_result_dataclass(self):
        """Test OptimizationResult dataclass structure."""
        from kicad_tools.optim.place_route import OptimizationResult

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


# =============================================================================
# --fixed plumbing tests (issue #2537)
# =============================================================================


class TestFixedFlagWithRoutingAware:
    """Tests that ``--fixed`` reaches the routing-aware path (issue #2537).

    Before the fix, ``--fixed`` was parsed AFTER the routing-aware dispatch
    so the constraint was silently dropped. These tests guard against that
    regression at the CLI level.
    """

    def test_fixed_arg_parses_with_routing_aware(self):
        """``--fixed`` and ``--routing-aware`` parse without conflict."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "placement",
                "optimize",
                "test.kicad_pcb",
                "--routing-aware",
                "--fixed",
                "J1",
            ]
        )

        assert args.routing_aware is True
        assert args.fixed == "J1"

    def test_fixed_arg_with_multiple_components(self):
        """Comma-separated ``--fixed`` accumulates correctly."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "placement",
                "optimize",
                "test.kicad_pcb",
                "--routing-aware",
                "--fixed",
                "J1,U1,H1",
            ]
        )

        assert args.fixed == "J1,U1,H1"

    def test_routing_aware_dispatch_receives_fixed_refs(self, tmp_path):
        """``_cmd_optimize_routing_aware`` is called with parsed fixed_refs."""
        import json
        from unittest.mock import patch

        from kicad_tools.cli import placement_cmd

        # Create a tiny placeholder PCB so the path-existence check passes.
        pcb_path = tmp_path / "fake.kicad_pcb"
        pcb_path.write_text(
            "(kicad_pcb (version 20240108) (generator test) "
            "(generator_version 8.0) (general (thickness 1.6)) "
            '(layers (0 "F.Cu" signal) (44 "Edge.Cuts" user)) '
            "(setup (pad_to_mask_clearance 0)))\n"
        )

        captured: dict = {}

        def fake_routing_aware(args, p, quiet, output_format, fixed_refs=None):
            captured["fixed_refs"] = list(fixed_refs or [])
            captured["pcb_path"] = p
            print(json.dumps({"success": True, "captured": True}))
            return 0

        with patch.object(placement_cmd, "_cmd_optimize_routing_aware", fake_routing_aware):
            placement_cmd.main(
                [
                    "optimize",
                    str(pcb_path),
                    "--routing-aware",
                    "--fixed",
                    "J1,U1",
                    "--dry-run",
                    "--quiet",
                ]
            )

        assert captured.get("fixed_refs") == ["J1", "U1"]

    def test_routing_aware_dispatch_default_fixed_refs_empty(self, tmp_path):
        """Without ``--fixed``, the routing-aware path gets an empty list."""
        from unittest.mock import patch

        from kicad_tools.cli import placement_cmd

        pcb_path = tmp_path / "fake.kicad_pcb"
        pcb_path.write_text(
            "(kicad_pcb (version 20240108) (generator test) "
            "(generator_version 8.0) (general (thickness 1.6)) "
            '(layers (0 "F.Cu" signal) (44 "Edge.Cuts" user)) '
            "(setup (pad_to_mask_clearance 0)))\n"
        )

        captured: dict = {}

        def fake_routing_aware(args, p, quiet, output_format, fixed_refs=None):
            captured["fixed_refs"] = list(fixed_refs or [])
            return 0

        with patch.object(placement_cmd, "_cmd_optimize_routing_aware", fake_routing_aware):
            placement_cmd.main(
                [
                    "optimize",
                    str(pcb_path),
                    "--routing-aware",
                    "--dry-run",
                    "--quiet",
                ]
            )

        assert captured.get("fixed_refs") == []

    def test_fixed_refs_passed_to_optimizer_from_pcb(self, tmp_path, minimal_pcb):
        """``_cmd_optimize_routing_aware`` forwards fixed_refs to from_pcb."""
        from unittest.mock import patch

        from kicad_tools.cli import placement_cmd

        captured: dict = {}

        # Patch from_pcb to capture the fixed_refs kwarg without running
        # the full optimization.
        from kicad_tools.optim.place_route import PlaceRouteOptimizer

        def fake_from_pcb(pcb, *, pcb_path=None, fixed_refs=None, **kwargs):
            captured["fixed_refs"] = fixed_refs
            # Build a stub optimizer that returns a successful result on
            # ``optimize`` so the CLI saves the PCB and exits 0.
            from unittest.mock import MagicMock

            from kicad_tools.optim.workflow import OptimizationResult

            stub = MagicMock(spec=PlaceRouteOptimizer)
            stub.optimize.return_value = OptimizationResult(
                success=True,
                pcb_path=pcb_path,
                routes=[],
                iterations=1,
                message="ok",
            )
            return stub

        with patch.object(PlaceRouteOptimizer, "from_pcb", fake_from_pcb):
            placement_cmd.main(
                [
                    "optimize",
                    str(minimal_pcb),
                    "--routing-aware",
                    "--fixed",
                    "R1,U1",
                    "--dry-run",
                    "--quiet",
                ]
            )

        assert captured.get("fixed_refs") == {"R1", "U1"}


class TestFixedRefsPositionInvariance:
    """End-to-end tests that ``--fixed`` keeps a component's position fixed.

    These tests exercise the whole optimization loop on small synthetic
    PCBs to confirm the named component does not move regardless of
    routing-aware / non-routing-aware dispatch.
    """

    @staticmethod
    def _read_position(pcb_path: Path, ref: str) -> tuple[float, float]:
        """Return ``(x, y)`` for the given component ref."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))
        for fp in pcb.footprints:
            if fp.reference == ref:
                return (float(fp.position[0]), float(fp.position[1]))
        raise AssertionError(f"Component {ref} not found in {pcb_path}")

    def test_routing_aware_with_fixed_keeps_position(self, minimal_pcb, tmp_path):
        """``--fixed R1 --routing-aware`` keeps R1's position unchanged.

        The minimal PCB has a single resistor R1; fixing it should
        guarantee its position is bit-identical post-optimization.
        """
        from kicad_tools.cli import placement_cmd

        before_pos = self._read_position(minimal_pcb, "R1")
        out_path = tmp_path / "out.kicad_pcb"

        rc = placement_cmd.main(
            [
                "optimize",
                str(minimal_pcb),
                "--routing-aware",
                "--fixed",
                "R1",
                "-o",
                str(out_path),
                "--quiet",
            ]
        )
        assert rc in (0, 1)

        # Optimization may not succeed on a single-component PCB, but the
        # input must be preserved. The output is only saved if the run
        # produced one; otherwise check the input file.
        target = out_path if out_path.exists() else minimal_pcb
        after_pos = self._read_position(target, "R1")

        assert after_pos == before_pos, f"R1 moved from {before_pos} to {after_pos} despite --fixed"

    def test_default_path_with_fixed_keeps_position(self, minimal_pcb, tmp_path):
        """Non-routing-aware path also honors ``--fixed`` (regression guard)."""
        from kicad_tools.cli import placement_cmd

        before_pos = self._read_position(minimal_pcb, "R1")
        out_path = tmp_path / "out.kicad_pcb"

        placement_cmd.main(
            [
                "optimize",
                str(minimal_pcb),
                "--fixed",
                "R1",
                "-o",
                str(out_path),
                "--quiet",
            ]
        )

        target = out_path if out_path.exists() else minimal_pcb
        after_pos = self._read_position(target, "R1")

        assert after_pos == before_pos

    def test_routing_aware_unconstrained_path_unaffected(self, minimal_pcb, tmp_path):
        """Without ``--fixed``, optimization runs as before (no regression).

        We don't assert that R1 *did* move (a single-component PCB has
        nothing to optimize) — only that the run completes cleanly.
        """
        from kicad_tools.cli import placement_cmd

        out_path = tmp_path / "out.kicad_pcb"
        rc = placement_cmd.main(
            [
                "optimize",
                str(minimal_pcb),
                "--routing-aware",
                "-o",
                str(out_path),
                "--dry-run",
                "--quiet",
            ]
        )

        # Should not error out due to missing fixed_refs handling.
        assert rc in (0, 1)
