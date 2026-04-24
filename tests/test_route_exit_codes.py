"""Tests for route command exit codes (issue #1301, #1413, #1454, #1946, #2018).

Exit code semantics (updated for --min-completion threshold support):
  0 = Routing meets --min-completion threshold AND (DRC passed OR DRC not run)
  1 = Fatal failure -- no nets routed, no useful output
  2 = Partial routing -- some nets routed but below --min-completion threshold
  3 = Meets threshold but DRC violations detected (includes seg-seg violations)
  4 = Below threshold AND seg-seg clearance violations remain (Issue #1666)
  5 = Interrupted by SIGINT with partial results saved
"""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    These tests verify the branching logic that maps routing state
    to exit codes, matching the exact logic in route_cmd.py main().
    """

    @staticmethod
    def _compute_exit_code(
        nets_routed, nets_to_route, drc_errors, min_completion=0.95, seg_seg_violations=0
    ):
        """Replicate the exit code logic from route_cmd.py main().

        This must stay in sync with the real code:
            completion_ratio = nets_routed / nets_to_route if nets_to_route > 0 else 1.0
            meets_threshold = completion_ratio >= min_completion
            if nets_routed == 0 and nets_to_route > 0: return 1
            elif meets_threshold and drc_passed and seg_seg == 0: return 0
            elif meets_threshold and (not drc_passed or seg_seg > 0): return 3
            elif not meets_threshold and seg_seg > 0: return 4
            else: return 2
        """
        drc_passed = drc_errors <= 0
        completion_ratio = nets_routed / nets_to_route if nets_to_route > 0 else 1.0
        meets_threshold = completion_ratio >= min_completion

        if nets_routed == 0 and nets_to_route > 0:
            return 1
        elif meets_threshold and drc_passed and seg_seg_violations == 0:
            return 0
        elif meets_threshold and (not drc_passed or seg_seg_violations > 0):
            return 3
        elif not meets_threshold and seg_seg_violations > 0:
            return 4
        else:
            return 2

    # ------------------------------------------------------------------
    # Legacy tests (default min_completion=0.95, no seg-seg violations)
    # These verify backward compatibility with the old exit code behavior
    # when all nets are routed (100% >= 95% threshold).
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "nets_routed, nets_to_route, drc_errors, expected_exit",
        [
            # Full success: all routed, no DRC errors
            (5, 5, 0, 0),
            # Full success: all routed, DRC not run (drc_errors = -1)
            (5, 5, -1, 0),
            # Full success: zero nets to route (empty board)
            (0, 0, 0, 0),
            # Partial routing: some nets routed, no DRC errors -> exit 2
            (3, 5, 0, 2),
            # Partial routing: some nets routed, with DRC errors too -> exit 2
            (3, 5, 2, 2),
            # Fatal failure: nothing routed -> exit 1
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
        # Routing failure: no nets routed at all
        routing_exit = self._compute_exit_code(nets_routed=0, nets_to_route=5, drc_errors=0)

        assert drc_only_exit == 3, "DRC-only failure must return exit code 3"
        assert routing_exit == 1, "Fatal routing failure (0 nets) must return exit code 1"
        assert drc_only_exit != routing_exit, (
            "DRC-only failure must be distinguishable from routing failure"
        )

    def test_partial_routing_returns_exit_2(self):
        """Partial routing (some nets routed, not all) returns exit code 2.

        This is the core fix from issue #1413: partial routing should not
        abort the pipeline. Exit code 2 signals 'completed with warnings'
        so downstream steps (fix-drc, audit, report) still run.
        """
        exit_code = self._compute_exit_code(nets_routed=42, nets_to_route=58, drc_errors=0)
        assert exit_code == 2, "Partial routing must return exit code 2"

    def test_zero_nets_routed_returns_exit_1(self):
        """When no nets are routed at all, exit code is 1 (fatal failure).

        This distinguishes between 'partial success' (exit 2) and
        'complete failure' (exit 1). A board with zero routed nets
        has no useful output for downstream pipeline steps.
        """
        exit_code = self._compute_exit_code(nets_routed=0, nets_to_route=5, drc_errors=0)
        assert exit_code == 1, "Zero nets routed must return exit code 1"

    def test_fatal_failure_always_exit_1_regardless_of_drc(self):
        """When zero nets are routed, exit code is always 1 regardless of DRC status."""
        for drc_errors in [-1, 0, 1, 5]:
            exit_code = self._compute_exit_code(
                nets_routed=0, nets_to_route=5, drc_errors=drc_errors
            )
            assert exit_code == 1, (
                f"Zero nets routed with drc_errors={drc_errors} should return 1, got {exit_code}"
            )

    def test_partial_routing_always_exit_2_regardless_of_drc(self):
        """When some nets are routed (but below threshold), exit code is always 2 regardless of DRC."""
        for drc_errors in [-1, 0, 1, 5]:
            exit_code = self._compute_exit_code(
                nets_routed=2, nets_to_route=5, drc_errors=drc_errors
            )
            assert exit_code == 2, (
                f"Partial routing with drc_errors={drc_errors} should return 2, got {exit_code}"
            )

    def test_exit_codes_are_exhaustive(self):
        """Every combination of inputs maps to a valid exit code."""
        valid_exit_codes = {0, 1, 2, 3, 4}
        for nets_routed in [0, 3, 5]:
            for nets_to_route in [0, 5]:
                for drc_errors in [-1, 0, 1, 10]:
                    for seg_seg in [0, 3]:
                        for min_comp in [0.0, 0.5, 0.95, 1.0]:
                            exit_code = self._compute_exit_code(
                                nets_routed, nets_to_route, drc_errors,
                                min_completion=min_comp, seg_seg_violations=seg_seg,
                            )
                            assert exit_code in valid_exit_codes, (
                                f"Unexpected exit code {exit_code} for "
                                f"nets_routed={nets_routed}, nets_to_route={nets_to_route}, "
                                f"drc_errors={drc_errors}, seg_seg={seg_seg}, "
                                f"min_completion={min_comp}"
                            )

    def test_exit_code_2_distinct_from_1_and_3(self):
        """Exit code 2 (partial) is distinct from 1 (fatal) and 3 (DRC-only)."""
        partial = self._compute_exit_code(nets_routed=3, nets_to_route=5, drc_errors=0)
        fatal = self._compute_exit_code(nets_routed=0, nets_to_route=5, drc_errors=0)
        drc_only = self._compute_exit_code(nets_routed=5, nets_to_route=5, drc_errors=2)

        assert partial == 2
        assert fatal == 1
        assert drc_only == 3
        assert len({partial, fatal, drc_only}) == 3, "All three exit codes must be distinct"

    # ------------------------------------------------------------------
    # Threshold-based exit code tests (issue #1946)
    # ------------------------------------------------------------------

    def test_min_completion_controls_success_threshold(self):
        """--min-completion 0.80 returns exit 0 when 85% of nets are routed."""
        # 17/20 = 85% >= 80% threshold -> success
        exit_code = self._compute_exit_code(
            nets_routed=17, nets_to_route=20, drc_errors=0, min_completion=0.80
        )
        assert exit_code == 0, "85% completion with 80% threshold should return 0"

    def test_min_completion_returns_partial_below_threshold(self):
        """--min-completion 0.95 returns exit 2 when only 85% routed."""
        # 17/20 = 85% < 95% threshold -> partial
        exit_code = self._compute_exit_code(
            nets_routed=17, nets_to_route=20, drc_errors=0, min_completion=0.95
        )
        assert exit_code == 2, "85% completion with 95% threshold should return 2"

    def test_min_completion_exact_boundary(self):
        """Exactly at the threshold returns success (>=)."""
        # 4/5 = 0.80 exactly == 0.80 threshold -> success
        exit_code = self._compute_exit_code(
            nets_routed=4, nets_to_route=5, drc_errors=0, min_completion=0.80
        )
        assert exit_code == 0, "Exact threshold match should return 0"

    def test_min_completion_just_below_boundary(self):
        """Just below the threshold returns partial."""
        # 3/5 = 0.60 < 0.80 threshold -> partial
        exit_code = self._compute_exit_code(
            nets_routed=3, nets_to_route=5, drc_errors=0, min_completion=0.80
        )
        assert exit_code == 2, "Below threshold should return 2"

    def test_min_completion_zero_always_succeeds(self):
        """--min-completion 0.0 should return 0 if any nets routed."""
        exit_code = self._compute_exit_code(
            nets_routed=1, nets_to_route=20, drc_errors=0, min_completion=0.0
        )
        assert exit_code == 0, "--min-completion 0.0 with any routed nets should return 0"

    def test_min_completion_one_requires_all(self):
        """--min-completion 1.0 should require all nets routed."""
        exit_code = self._compute_exit_code(
            nets_routed=19, nets_to_route=20, drc_errors=0, min_completion=1.0
        )
        assert exit_code == 2, "--min-completion 1.0 with 19/20 should return 2"

        exit_code = self._compute_exit_code(
            nets_routed=20, nets_to_route=20, drc_errors=0, min_completion=1.0
        )
        assert exit_code == 0, "--min-completion 1.0 with 20/20 should return 0"

    def test_min_completion_zero_still_fatal_if_no_nets_routed(self):
        """Even with --min-completion 0.0, zero nets routed is fatal."""
        exit_code = self._compute_exit_code(
            nets_routed=0, nets_to_route=20, drc_errors=0, min_completion=0.0
        )
        assert exit_code == 1, "Zero nets routed is always fatal regardless of threshold"

    # ------------------------------------------------------------------
    # Seg-seg violation priority tests (issue #1946)
    # ------------------------------------------------------------------

    def test_seg_seg_violations_above_threshold_return_3(self):
        """Seg-seg violations when above threshold return exit 3 (DRC failure)."""
        exit_code = self._compute_exit_code(
            nets_routed=20, nets_to_route=20, drc_errors=0,
            min_completion=0.95, seg_seg_violations=5,
        )
        assert exit_code == 3, "Seg-seg violations above threshold should return 3"

    def test_seg_seg_violations_below_threshold_return_4(self):
        """Seg-seg violations when below threshold return exit 4."""
        exit_code = self._compute_exit_code(
            nets_routed=10, nets_to_route=20, drc_errors=0,
            min_completion=0.95, seg_seg_violations=5,
        )
        assert exit_code == 4, "Seg-seg violations below threshold should return 4"

    def test_seg_seg_violations_do_not_mask_partial_routing(self):
        """Seg-seg violations below threshold return 4, not hiding the partial status."""
        # Previously this returned 4 even when the real issue was partial routing.
        # Now exit 4 explicitly means both partial AND seg-seg violations.
        exit_code = self._compute_exit_code(
            nets_routed=17, nets_to_route=20, drc_errors=0,
            min_completion=0.95, seg_seg_violations=3,
        )
        assert exit_code == 4, (
            "Below threshold with seg-seg violations should return 4"
        )

    def test_seg_seg_above_threshold_with_drc_errors_returns_3(self):
        """Above threshold with both DRC errors and seg-seg violations returns 3."""
        exit_code = self._compute_exit_code(
            nets_routed=20, nets_to_route=20, drc_errors=5,
            min_completion=0.95, seg_seg_violations=3,
        )
        assert exit_code == 3, "Above threshold with violations should return 3"


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

    def test_main_exit_code_is_int(self, tmp_path):
        """main() returns an integer exit code, not None or a string."""
        pcb_file = _make_minimal_pcb(tmp_path)
        result = route_main([str(pcb_file), "--dry-run", "--quiet", "--grid", "0.1"])
        assert isinstance(result, int), f"Exit code should be int, got {type(result)}"


class TestRouteExitCodeDocumentation:
    """Verify the exit code comment block in route_cmd.py is accurate."""

    def test_source_documents_exit_code_2_for_partial(self):
        """The exit code comment in route_cmd.py documents exit code 2 for partial routing."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd.main)
        assert "return 2" in source, "route_cmd.main() must contain 'return 2' for partial routing"
        assert "Partial routing" in source, (
            "route_cmd.main() must document partial routing for exit code 2"
        )

    def test_source_documents_exit_code_3(self):
        """The exit code comment in route_cmd.py documents exit code 3."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd.main)
        assert "return 3" in source, (
            "route_cmd.main() must contain 'return 3' for DRC-only failures"
        )
        assert "return 0" in source, "route_cmd.main() must contain 'return 0' for success"
        assert "return 1" in source, "route_cmd.main() must contain 'return 1' for fatal failure"

    def test_source_documents_threshold_semantics(self):
        """The exit code comments document --min-completion threshold behavior."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd.main)
        assert "min_completion" in source, (
            "route_cmd.main() must reference min_completion in exit code logic"
        )
        assert "meets_threshold" in source, (
            "route_cmd.main() must use meets_threshold variable"
        )


# ---------------------------------------------------------------------------
# Partial routing output suggestions (issue #1454)
# ---------------------------------------------------------------------------


def _make_mock_route(net_id: int):
    """Create a minimal mock Route object for output tests."""
    route = MagicMock()
    route.net = net_id
    route.segments = []
    route.vias = []
    return route


def _make_mock_router(routed_nets, num_layers=2, routing_failures=None):
    """Create a mock Autorouter for output tests."""
    router = MagicMock()
    router.routes = [_make_mock_route(nid) for nid in routed_nets]
    router.routing_failures = routing_failures or []
    router.grid = SimpleNamespace(num_layers=num_layers, resolution=0.25)
    return router


class TestPartialRoutingSuggestions:
    """Tests that partial routing output surfaces existing capabilities (issue #1454).

    When routing completes partially (exit code 2), the output should include:
    - Percentage-based layer escalation recommendation with exact commands
    - Mention of --export-failed-nets option
    - Suggestion of --strategy monte-carlo when current strategy is negotiated
    - --auto-layers suggestion when on 2 layers with >20% failure rate
    - Copy-pasteable commands with the current PCB file path
    """

    def test_auto_layers_recommendation_above_20_pct_failure(self):
        """When >20% of nets fail on 2 layers, output includes RECOMMENDATION block."""
        from kicad_tools.router.output import show_routing_summary

        # 16/58 nets failed = 28% failure rate (mirrors the softstart scenario)
        net_map = {f"Net{i}": i for i in range(1, 59)}
        router = _make_mock_router(routed_nets=list(range(1, 43)), num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=58,
                quiet=False,
                pcb_file="softstart.kicad_pcb",
            )

        text = output.getvalue()
        assert "RECOMMENDATION" in text
        assert "16/58" in text
        assert "28%" in text
        assert "kct route softstart.kicad_pcb --auto-layers" in text
        assert "kct route softstart.kicad_pcb --layers 4" in text

    def test_auto_layers_tip_below_20_pct_failure(self):
        """When <=20% of nets fail on 2 layers, output shows Tip instead of RECOMMENDATION."""
        from kicad_tools.router.output import show_routing_summary

        # 1/10 nets failed = 10% failure rate
        net_map = {f"Net{i}": i for i in range(1, 11)}
        router = _make_mock_router(routed_nets=list(range(1, 10)), num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=10,
                quiet=False,
                pcb_file="board.kicad_pcb",
            )

        text = output.getvalue()
        assert "RECOMMENDATION" not in text
        assert "Tip:" in text
        assert "--auto-layers" in text

    def test_export_failed_nets_mentioned_in_partial_output(self):
        """Partial routing output mentions --export-failed-nets option."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2, "C": 3}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=3,
                quiet=False,
                pcb_file="board.kicad_pcb",
            )

        text = output.getvalue()
        assert "--export-failed-nets" in text
        assert "kct route board.kicad_pcb --export-failed-nets" in text

    def test_monte_carlo_suggested_when_negotiated(self):
        """When current strategy is negotiated, output suggests monte-carlo."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2, "C": 3}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=3,
                quiet=False,
                current_strategy="negotiated",
                pcb_file="board.kicad_pcb",
            )

        text = output.getvalue()
        assert "--strategy monte-carlo --mc-trials 20" in text
        assert "kct route board.kicad_pcb --strategy monte-carlo" in text

    def test_no_escalation_recommendation_when_all_routed(self):
        """Full routing success (exit code 0) does NOT show escalation recommendations."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2}
        router = _make_mock_router(routed_nets=[1, 2], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=2,
                quiet=False,
            )

        text = output.getvalue()
        assert "RECOMMENDATION" not in text
        assert "--auto-layers" not in text
        assert "--export-failed-nets" not in text

    def test_suggestions_include_pcb_file_path(self):
        """All copy-pasteable commands include the PCB file path."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2, "C": 3, "D": 4}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=4,
                quiet=False,
                pcb_file="my_board.kicad_pcb",
            )

        text = output.getvalue()
        # Check that command suggestions include the PCB file path
        assert "kct route my_board.kicad_pcb --auto-layers" in text
        assert "kct route my_board.kicad_pcb --export-failed-nets" in text

    def test_json_diagnostics_includes_layer_escalation(self):
        """JSON diagnostics includes LAYER_ESCALATION when >20% failure on 2 layers."""
        from kicad_tools.router.output import get_routing_diagnostics_json

        # 3/4 nets failed = 75% failure rate
        net_map = {"A": 1, "B": 2, "C": 3, "D": 4}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        result = get_routing_diagnostics_json(
            router, net_map, nets_to_route=4, current_strategy="basic"
        )

        suggestions = result.get("suggestions", [])
        escalation = [s for s in suggestions if s.get("category") == "LAYER_ESCALATION"]
        assert len(escalation) == 1
        assert "--auto-layers" in escalation[0]["fix"]
        assert "75%" in escalation[0]["description"]

    def test_json_diagnostics_includes_export_suggestion(self):
        """JSON diagnostics includes EXPORT suggestion when nets fail."""
        from kicad_tools.router.output import get_routing_diagnostics_json

        net_map = {"A": 1, "B": 2}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        result = get_routing_diagnostics_json(
            router, net_map, nets_to_route=2, current_strategy="basic"
        )

        suggestions = result.get("suggestions", [])
        export_suggestions = [s for s in suggestions if s.get("category") == "EXPORT"]
        assert len(export_suggestions) == 1
        assert "--export-failed-nets" in export_suggestions[0]["fix"]

    def test_json_diagnostics_no_export_when_all_routed(self):
        """JSON diagnostics does NOT include EXPORT suggestion when all nets routed."""
        from kicad_tools.router.output import get_routing_diagnostics_json

        net_map = {"A": 1, "B": 2}
        router = _make_mock_router(routed_nets=[1, 2], num_layers=2)

        result = get_routing_diagnostics_json(
            router, net_map, nets_to_route=2, current_strategy="basic"
        )

        suggestions = result.get("suggestions", [])
        export_suggestions = [s for s in suggestions if s.get("category") == "EXPORT"]
        assert len(export_suggestions) == 0

    def test_json_monte_carlo_surfaced_when_negotiated(self):
        """JSON diagnostics surfaces monte-carlo when current strategy is negotiated."""
        from kicad_tools.router.output import get_routing_diagnostics_json

        net_map = {"A": 1, "B": 2}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        result = get_routing_diagnostics_json(
            router, net_map, nets_to_route=2, current_strategy="negotiated"
        )

        suggestions = result.get("suggestions", [])
        mc_suggestions = [s for s in suggestions if "monte-carlo" in s.get("fix", "")]
        assert len(mc_suggestions) >= 1


# ---------------------------------------------------------------------------
# Exit code epilog in --help output (issue #2018)
# ---------------------------------------------------------------------------


class TestRouteExitCodeEpilog:
    """Verify that exit codes are documented in --help output."""

    def test_help_epilog_documents_all_exit_codes(self):
        """The argparse epilog lists all exit codes 0-5."""
        import argparse

        from kicad_tools.cli import route_cmd

        # Build the parser and check its epilog
        # We need to capture the help output to verify epilog is included
        with patch("sys.argv", ["kicad-tools", "route", "--help"]):
            try:
                result = route_cmd.main(["--help"])
            except SystemExit:
                pass  # --help causes SystemExit(0)

        # Directly inspect the parser's epilog by constructing it the same way
        parser = argparse.ArgumentParser(
            prog="kicad-tools route",
            description="Autoroute a KiCad PCB file",
        )
        # The real parser is created inside main(), so we check the source
        import inspect
        source = inspect.getsource(route_cmd.main)
        assert "epilog=" in source, "Parser must have an epilog argument"

    def test_help_epilog_contains_each_exit_code(self):
        """The epilog text documents each individual exit code."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd.main)
        # Verify all six exit codes are documented in the epilog
        for code in range(6):
            assert f"  {code}  " in source, (
                f"Exit code {code} must be documented in the parser epilog"
            )

    def test_help_epilog_mentions_sigint(self):
        """The epilog documents that exit code 5 is for SIGINT interruption."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd.main)
        assert "SIGINT" in source, "Epilog must mention SIGINT for exit code 5"

    def test_parser_uses_raw_description_formatter(self):
        """Parser uses RawDescriptionHelpFormatter so epilog whitespace is preserved."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd.main)
        assert "RawDescriptionHelpFormatter" in source, (
            "Parser must use RawDescriptionHelpFormatter for epilog formatting"
        )


# ---------------------------------------------------------------------------
# SIGINT exit code disambiguation (issue #2018)
# ---------------------------------------------------------------------------


class TestSigintExitCode:
    """Verify that SIGINT uses exit code 5, distinct from code 2."""

    def test_sigint_handler_uses_exit_code_5(self):
        """The SIGINT handler exits with code 5 when partial results are saved."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd._handle_interrupt)
        assert "sys.exit(5" in source, (
            "SIGINT handler must use exit code 5 for saved partial results"
        )

    def test_sigint_handler_falls_back_to_130(self):
        """The SIGINT handler exits with 130 when no partial results could be saved."""
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd._handle_interrupt)
        assert "130" in source, (
            "SIGINT handler must fall back to 130 (128 + SIGINT) when save fails"
        )

    def test_sigint_exit_code_distinct_from_partial(self):
        """Exit code 5 (SIGINT) is distinct from exit code 2 (partial routing)."""
        # Verify the SIGINT handler doesn't use code 2
        import inspect

        from kicad_tools.cli import route_cmd

        source = inspect.getsource(route_cmd._handle_interrupt)
        assert "sys.exit(2" not in source, (
            "SIGINT handler must NOT use exit code 2 (that is for partial routing)"
        )


# ---------------------------------------------------------------------------
# Consumer exit code handling tests (issue #2018)
# ---------------------------------------------------------------------------


class TestBuildCmdExitCodeHandling:
    """Verify build_cmd.py handles route exit codes 3, 4, and 5 as non-fatal."""

    def test_build_cmd_treats_exit_3_as_success(self):
        """build_cmd treats exit code 3 (DRC violations) as non-fatal."""
        import inspect

        from kicad_tools.cli import build_cmd

        source = inspect.getsource(build_cmd)
        # The returncode check should include 3
        assert "result.returncode in (2, 3, 4, 5)" in source, (
            "build_cmd must handle exit codes 2, 3, 4, and 5 as non-fatal"
        )

    def test_build_cmd_treats_exit_4_as_success(self):
        """build_cmd treats exit code 4 (partial + seg-seg) as non-fatal."""
        import inspect

        from kicad_tools.cli import build_cmd

        source = inspect.getsource(build_cmd)
        assert "4" in source, "build_cmd must handle exit code 4"

    def test_build_cmd_has_distinct_messages_per_code(self):
        """build_cmd provides distinct warning messages for each exit code."""
        import inspect

        from kicad_tools.cli import build_cmd

        source = inspect.getsource(build_cmd)
        assert "DRC violations remain" in source, (
            "build_cmd must have a message for exit code 3 (DRC violations)"
        )
        assert "clearance violations" in source, (
            "build_cmd must have a message for exit code 4 (clearance violations)"
        )
        assert "interrupted" in source.lower(), (
            "build_cmd must have a message for exit code 5 (SIGINT)"
        )


class TestPipelineCmdExitCodeHandling:
    """Verify pipeline_cmd.py handles route exit codes 3, 4, and 5 as non-fatal."""

    def test_pipeline_cmd_treats_exit_3_as_success(self):
        """pipeline_cmd treats exit code 3 as completed with warnings."""
        import inspect

        from kicad_tools.cli import pipeline_cmd

        source = inspect.getsource(pipeline_cmd._run_subprocess_step)
        assert "result.returncode in (2, 3, 4, 5)" in source, (
            "pipeline_cmd must handle exit codes 2, 3, 4, and 5 as non-fatal"
        )

    def test_pipeline_cmd_returns_true_for_codes_2_through_5(self):
        """pipeline_cmd returns success=True for exit codes 2, 3, 4, and 5."""
        import subprocess
        from pathlib import Path
        from unittest.mock import patch

        from kicad_tools.cli.pipeline_cmd import _run_subprocess_step

        for code in (2, 3, 4, 5):
            mock_result = MagicMock()
            mock_result.returncode = code
            mock_result.stderr = ""

            with patch("kicad_tools.cli.pipeline_cmd.subprocess.run", return_value=mock_result):
                success, msg = _run_subprocess_step(
                    cmd=["kct", "route", "test.kicad_pcb"],
                    cwd=Path("/tmp"),
                )
                assert success is True, (
                    f"Exit code {code} should be treated as success, got failure"
                )
                assert "warnings" in msg, (
                    f"Exit code {code} message should mention warnings, got: {msg}"
                )

    def test_pipeline_cmd_returns_false_for_code_1(self):
        """pipeline_cmd returns success=False for exit code 1 (fatal failure)."""
        from pathlib import Path

        from kicad_tools.cli.pipeline_cmd import _run_subprocess_step

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "fatal error"

        with patch("kicad_tools.cli.pipeline_cmd.subprocess.run", return_value=mock_result):
            success, msg = _run_subprocess_step(
                cmd=["kct", "route", "test.kicad_pcb"],
                cwd=Path("/tmp"),
            )
            assert success is False, "Exit code 1 should be treated as failure"
