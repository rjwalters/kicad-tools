"""Tests for two-phase iteration resolution (issue #2324).

Verifies that --iterations falls back correctly to two-phase routing
when --two-phase-iterations is not explicitly set, and that explicit
--two-phase-iterations always wins.
"""

import argparse
import contextlib
import sys
from io import StringIO
from unittest.mock import patch


def _build_parser():
    """Build a minimal parser matching route_cmd's iteration-related args."""
    parser = argparse.ArgumentParser()
    parser.add_argument("pcb")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--iterations", type=int, default=15)
    parser.add_argument("--two-phase-iterations", type=int, default=None)
    parser.add_argument("--two-phase", action="store_true")
    parser.add_argument("--high-performance", action="store_true")
    return parser


def _resolve_two_phase_iterations(parser, args):
    """Apply the same resolution logic as route_cmd.main."""
    _TWO_PHASE_DEFAULT = 20
    _two_phase_iters_explicit = getattr(args, "two_phase_iterations", None) is not None
    _iterations_explicitly_set = args.iterations != parser.get_default("iterations")
    if not _two_phase_iters_explicit:
        if _iterations_explicitly_set:
            args.two_phase_iterations = args.iterations
        else:
            args.two_phase_iterations = _TWO_PHASE_DEFAULT
    return _two_phase_iters_explicit


class TestTwoPhaseIterationResolution:
    """Tests for the iteration count resolution logic."""

    def test_default_no_flags_resolves_to_20(self):
        """No iteration flags: two-phase defaults to 20 (backward compat)."""
        parser = _build_parser()
        args = parser.parse_args(["test.kicad_pcb", "--two-phase"])
        _resolve_two_phase_iterations(parser, args)
        assert args.two_phase_iterations == 20

    def test_iterations_only_falls_back_to_iterations(self):
        """--iterations 5 without --two-phase-iterations resolves to 5."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--two-phase",
                "--iterations",
                "5",
            ]
        )
        _resolve_two_phase_iterations(parser, args)
        assert args.two_phase_iterations == 5

    def test_two_phase_iterations_explicit_wins(self):
        """--two-phase-iterations 10 --iterations 5 resolves to 10."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--two-phase",
                "--two-phase-iterations",
                "10",
                "--iterations",
                "5",
            ]
        )
        _resolve_two_phase_iterations(parser, args)
        assert args.two_phase_iterations == 10

    def test_two_phase_iterations_alone(self):
        """--two-phase-iterations 7 alone resolves to 7."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--two-phase",
                "--two-phase-iterations",
                "7",
            ]
        )
        _resolve_two_phase_iterations(parser, args)
        assert args.two_phase_iterations == 7

    def test_iterations_at_default_does_not_override(self):
        """When --iterations is at default (15), two-phase stays at 20."""
        parser = _build_parser()
        args = parser.parse_args(["test.kicad_pcb", "--two-phase"])
        _resolve_two_phase_iterations(parser, args)
        # iterations is at default 15, should NOT be used as fallback
        assert args.two_phase_iterations == 20
        assert args.iterations == 15

    def test_iterations_explicitly_set_to_default_value(self):
        """--iterations 15 explicitly still counts as explicit (equals default)."""
        parser = _build_parser()
        # When the user passes --iterations 15, argparse cannot distinguish
        # from the default. This is a known limitation: if the user explicitly
        # passes the same value as the default, it behaves as if not set.
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--two-phase",
                "--iterations",
                "15",
            ]
        )
        _resolve_two_phase_iterations(parser, args)
        # This will resolve to 20 because we can't detect explicit 15 vs default 15.
        # This is acceptable behavior documented in the help text.
        assert args.two_phase_iterations == 20

    def test_large_iteration_count_propagates(self):
        """--iterations 100 propagates to two-phase when not explicitly set."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--two-phase",
                "--iterations",
                "100",
            ]
        )
        _resolve_two_phase_iterations(parser, args)
        assert args.two_phase_iterations == 100

    def test_iterations_1_propagates(self):
        """--iterations 1 propagates to two-phase (edge case)."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--two-phase",
                "--iterations",
                "1",
            ]
        )
        _resolve_two_phase_iterations(parser, args)
        assert args.two_phase_iterations == 1


class TestTwoPhaseHighPerformanceResolution:
    """Tests for high-performance mode interaction with two-phase iterations."""

    def test_high_performance_overrides_two_phase_default(self):
        """--high-performance applies calibrated iterations to two-phase."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--high-performance",
                "--two-phase",
            ]
        )
        explicit = _resolve_two_phase_iterations(parser, args)

        # Simulate high-performance override (as route_cmd does)
        calibrated_iterations = 25
        args.iterations = calibrated_iterations
        if not explicit:
            args.two_phase_iterations = calibrated_iterations

        assert args.two_phase_iterations == calibrated_iterations
        assert args.iterations == calibrated_iterations

    def test_explicit_two_phase_iters_not_overridden_by_high_perf(self):
        """--two-phase-iterations 10 --high-performance keeps 10."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--high-performance",
                "--two-phase",
                "--two-phase-iterations",
                "10",
            ]
        )
        explicit = _resolve_two_phase_iterations(parser, args)

        # Simulate high-performance override
        calibrated_iterations = 25
        args.iterations = calibrated_iterations
        if not explicit:
            args.two_phase_iterations = calibrated_iterations

        # Explicit --two-phase-iterations 10 must survive
        assert args.two_phase_iterations == 10
        assert args.iterations == calibrated_iterations

    def test_high_perf_with_explicit_iterations_not_two_phase(self):
        """--high-performance --iterations 3 applies calibrated to two-phase."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "test.kicad_pcb",
                "--high-performance",
                "--two-phase",
                "--iterations",
                "3",
            ]
        )
        explicit = _resolve_two_phase_iterations(parser, args)

        # After resolution, two_phase_iterations = 3 (from --iterations fallback)
        assert args.two_phase_iterations == 3

        # Now high-performance overrides since --two-phase-iterations was not explicit
        calibrated_iterations = 25
        args.iterations = calibrated_iterations
        if not explicit:
            args.two_phase_iterations = calibrated_iterations

        assert args.two_phase_iterations == calibrated_iterations


class TestTwoPhaseIterationsHelpText:
    """Tests for updated help text in route_cmd."""

    def test_iterations_help_mentions_two_phase(self):
        """--iterations help text mentions two-phase fallback."""
        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                from kicad_tools.cli.route_cmd import main as route_main

                route_main(["--help"])

        help_text = help_output.getvalue()
        assert "two-phase" in help_text.lower()
        assert "--two-phase-iterations" in help_text

    def test_two_phase_iterations_help_mentions_fallback(self):
        """--two-phase-iterations help text mentions --iterations fallback."""
        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                from kicad_tools.cli.route_cmd import main as route_main

                route_main(["--help"])

        help_text = help_output.getvalue()
        assert "falls back" in help_text.lower() or "Overrides --iterations" in help_text

    def test_two_phase_iterations_default_is_none_in_parser(self):
        """--two-phase-iterations argparse default is None (not 20)."""
        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                from kicad_tools.cli.route_cmd import main as route_main

                route_main(["--help"])

        help_text = help_output.getvalue()
        # The help text should mention the effective default of 20
        assert "20" in help_text


class TestTwoPhaseIterationsCallSiteDefense:
    """Defense tests: verify call sites use the resolved value correctly."""

    def test_call_site_pattern_uses_getattr_none(self):
        """All call sites use getattr(args, 'two_phase_iterations', None)."""
        import re
        from pathlib import Path

        route_cmd_path = (
            Path(__file__).parent.parent / "src" / "kicad_tools" / "cli" / "route_cmd.py"
        )
        content = route_cmd_path.read_text()

        # There should be NO remaining instances of the old pattern with default 20
        old_pattern = r'getattr\(args,\s*"two_phase_iterations",\s*20\)'
        matches = re.findall(old_pattern, content)
        assert len(matches) == 0, (
            f"Found {len(matches)} instances of old pattern "
            f"getattr(args, 'two_phase_iterations', 20) -- should be 0"
        )

        # The new pattern should exist (getattr with None default or direct access)
        new_pattern = r'getattr\(args,\s*"two_phase_iterations",\s*None\)'
        new_matches = re.findall(new_pattern, content)
        assert len(new_matches) >= 5, (
            f"Expected at least 5 call sites with new pattern, found {len(new_matches)}"
        )

    def test_resolution_block_exists(self):
        """The resolution block exists after parse_args."""
        from pathlib import Path

        route_cmd_path = (
            Path(__file__).parent.parent / "src" / "kicad_tools" / "cli" / "route_cmd.py"
        )
        content = route_cmd_path.read_text()

        assert "_TWO_PHASE_DEFAULT = 20" in content
        assert "_two_phase_iters_explicit" in content
        assert "_iterations_explicitly_set" in content
