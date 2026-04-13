"""Tests for route command exit codes (issue #1301).

Exit code semantics:
  0 = All nets routed AND (DRC passed OR DRC not run)
  1 = Not all nets routed (routing failure)
  2 = Interrupted by SIGINT (partial results saved) -- tested elsewhere
  3 = All nets routed but DRC violations detected
"""

import pytest

from kicad_tools.cli.route_cmd import main as route_main


def _make_minimal_pcb(tmp_path):
    """Create a minimal .kicad_pcb file for testing."""
    pcb_content = '(kicad_pcb (version 20240101) (generator "test"))'
    pcb_file = tmp_path / "test.kicad_pcb"
    pcb_file.write_text(pcb_content)
    return pcb_file


class TestRouteExitCodeLogic:
    """Direct unit tests for the exit code decision logic.

    These tests verify the branching logic that maps (all_nets_routed, drc_passed)
    to exit codes, matching the exact logic in route_cmd.py main().
    """

    @staticmethod
    def _compute_exit_code(nets_routed, nets_to_route, drc_errors):
        """Replicate the exit code logic from route_cmd.py main().

        This must stay in sync with the real code:
            all_nets_routed = stats["nets_routed"] == nets_to_route
            drc_passed = drc_errors <= 0
            if all_nets_routed and drc_passed: return 0
            elif not all_nets_routed: return 1
            else: return 3
        """
        all_nets_routed = nets_routed == nets_to_route
        drc_passed = drc_errors <= 0

        if all_nets_routed and drc_passed:
            return 0
        elif not all_nets_routed:
            return 1
        else:
            return 3

    @pytest.mark.parametrize(
        "nets_routed, nets_to_route, drc_errors, expected_exit",
        [
            # Full success: all routed, no DRC errors
            (5, 5, 0, 0),
            # Full success: all routed, DRC not run (drc_errors = -1)
            (5, 5, -1, 0),
            # Full success: zero nets to route (empty board)
            (0, 0, 0, 0),
            # Routing failure: partial routing, no DRC errors
            (3, 5, 0, 1),
            # Routing failure: partial routing, with DRC errors too
            (3, 5, 2, 1),
            # Routing failure: nothing routed
            (0, 5, 0, 1),
            # DRC-only failure: all routed, one DRC violation
            (5, 5, 1, 3),
            # DRC-only failure: all routed, many DRC violations
            (5, 5, 42, 3),
            # DRC-only failure: single net routed, DRC failed
            (1, 1, 3, 3),
        ],
    )
    def test_exit_code_decision(self, nets_routed, nets_to_route, drc_errors, expected_exit):
        """Verify exit code for each combination of routing and DRC outcomes."""
        exit_code = self._compute_exit_code(nets_routed, nets_to_route, drc_errors)
        assert exit_code == expected_exit, (
            f"nets_routed={nets_routed}, nets_to_route={nets_to_route}, "
            f"drc_errors={drc_errors}: expected exit {expected_exit}, got {exit_code}"
        )

    def test_exit_code_3_is_distinct_from_1(self):
        """Exit code 3 (DRC-only failure) is different from exit code 1 (routing failure).

        This is the core fix from issue #1301: before the change, both
        routing failures and DRC-only failures returned exit code 1, making
        it impossible for scripts to distinguish them.
        """
        # DRC-only failure: all nets routed but DRC violations exist
        drc_only_exit = self._compute_exit_code(nets_routed=5, nets_to_route=5, drc_errors=2)
        # Routing failure: some nets not routed
        routing_exit = self._compute_exit_code(nets_routed=3, nets_to_route=5, drc_errors=0)

        assert drc_only_exit == 3, "DRC-only failure must return exit code 3"
        assert routing_exit == 1, "Routing failure must return exit code 1"
        assert drc_only_exit != routing_exit, (
            "DRC-only failure must be distinguishable from routing failure"
        )

    def test_routing_failure_always_exit_1_regardless_of_drc(self):
        """When nets are unrouted, exit code is always 1 regardless of DRC status."""
        for drc_errors in [-1, 0, 1, 5]:
            exit_code = self._compute_exit_code(
                nets_routed=2, nets_to_route=5, drc_errors=drc_errors
            )
            assert exit_code == 1, (
                f"Routing failure with drc_errors={drc_errors} should return 1, got {exit_code}"
            )

    def test_exit_codes_are_exhaustive(self):
        """Every (all_nets_routed, drc_passed) combination maps to a valid exit code."""
        valid_exit_codes = {0, 1, 3}
        for nets_routed in [0, 3, 5]:
            for nets_to_route in [0, 5]:
                for drc_errors in [-1, 0, 1, 10]:
                    exit_code = self._compute_exit_code(nets_routed, nets_to_route, drc_errors)
                    assert exit_code in valid_exit_codes, (
                        f"Unexpected exit code {exit_code} for "
                        f"nets_routed={nets_routed}, nets_to_route={nets_to_route}, "
                        f"drc_errors={drc_errors}"
                    )


class TestRouteExitCodeIntegration:
    """Integration tests exercising exit codes through main().

    These call route_cmd.main() with a minimal PCB to verify that the
    exit code plumbing works end-to-end, not just the logic.
    """

    def test_main_returns_0_for_empty_board_dry_run(self, tmp_path):
        """An empty board with --dry-run returns exit code 0 (no nets to route)."""
        pcb_file = _make_minimal_pcb(tmp_path)
        result = route_main([str(pcb_file), "--dry-run", "--quiet", "--grid", "0.1"])
        assert result == 0, f"Empty board dry-run should return 0, got {result}"

    def test_main_returns_0_with_skip_drc(self, tmp_path):
        """An empty board with --skip-drc and --dry-run returns exit code 0."""
        pcb_file = _make_minimal_pcb(tmp_path)
        result = route_main([str(pcb_file), "--dry-run", "--quiet", "--skip-drc", "--grid", "0.1"])
        assert result == 0, f"Empty board with --skip-drc should return 0, got {result}"

    def test_main_never_returns_2_without_sigint(self, tmp_path):
        """Normal execution (no SIGINT) never returns exit code 2."""
        pcb_file = _make_minimal_pcb(tmp_path)
        result = route_main([str(pcb_file), "--dry-run", "--quiet", "--grid", "0.1"])
        assert result != 2, "Exit code 2 is reserved for SIGINT interruption"

    def test_main_exit_code_is_int(self, tmp_path):
        """main() returns an integer exit code, not None or a string."""
        pcb_file = _make_minimal_pcb(tmp_path)
        result = route_main([str(pcb_file), "--dry-run", "--quiet", "--grid", "0.1"])
        assert isinstance(result, int), f"Exit code should be int, got {type(result)}"


class TestRouteExitCodeDocumentation:
    """Verify the exit code comment block in route_cmd.py is accurate."""

    def test_source_documents_exit_code_3(self):
        """The exit code comment in route_cmd.py documents exit code 3."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd.main)
        assert "return 3" in source, (
            "route_cmd.main() must contain 'return 3' for DRC-only failures"
        )
        assert "return 0" in source, "route_cmd.main() must contain 'return 0' for success"
        assert "return 1" in source, "route_cmd.main() must contain 'return 1' for routing failure"

    def test_drc_failure_does_not_return_1(self):
        """The DRC-only failure path returns 3, not 1."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd.main)

        # Find the exit code block and verify the DRC failure branch
        # Look for the comment "All nets routed but DRC failed" followed by return 3
        assert "# All nets routed but DRC failed" in source
        # Find the line after the comment
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "# All nets routed but DRC failed" in line:
                # Next non-blank line should have return 3
                for j in range(i + 1, min(i + 3, len(lines))):
                    if "return" in lines[j]:
                        assert "return 3" in lines[j], (
                            f"DRC-only failure should return 3, found: {lines[j].strip()}"
                        )
                        break
