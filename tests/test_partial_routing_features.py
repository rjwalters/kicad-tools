"""Tests for partial routing UX improvements (issue #1382).

Covers three features:
1. Proactive --auto-layers suggestion in show_routing_summary() when nets fail
2. Partial result saving on clean partial exit (not just SIGINT)
3. --export-failed-nets CLI flag
"""

import contextlib
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_mock_route(net_id: int, net_name: str = ""):
    """Create a minimal mock Route object."""
    route = MagicMock()
    route.net = net_id
    route.net_name = net_name
    route.segments = []
    route.vias = []
    return route


def _make_mock_router(
    routed_nets: list[int],
    num_layers: int = 2,
    routing_failures: list | None = None,
):
    """Create a mock Autorouter with the given routed nets and grid config."""
    router = MagicMock()
    router.routes = [_make_mock_route(nid) for nid in routed_nets]
    router.routing_failures = routing_failures or []
    router.grid = SimpleNamespace(num_layers=num_layers, resolution=0.25)
    return router


class TestAutoLayersSuggestion:
    """Tests for proactive --auto-layers suggestion in show_routing_summary()."""

    def test_auto_layers_suggestion_shown_on_2_layer_failure(self):
        """Suggestion appears when nets fail on a 2-layer board."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2, "C": 3, "D": 4}
        router = _make_mock_router(routed_nets=[1, 2], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=4,
                quiet=False,
                pcb_file="board.kicad_pcb",
            )

        text = output.getvalue()
        assert "--auto-layers" in text
        assert "board.kicad_pcb" in text
        assert "2/4 nets failed" in text

    def test_auto_layers_suggestion_not_shown_on_4_layer_board(self):
        """Suggestion does NOT appear when already using 4+ layers."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2, "C": 3}
        router = _make_mock_router(routed_nets=[1], num_layers=4)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=3,
                quiet=False,
            )

        text = output.getvalue()
        assert "--auto-layers" not in text

    def test_auto_layers_suggestion_not_shown_when_all_routed(self):
        """Suggestion does NOT appear when all nets are routed (no failures)."""
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
        # No unrouted nets, so no suggestions block at all
        assert "--auto-layers" not in text

    def test_auto_layers_suggestion_shows_failure_percentage(self):
        """Suggestion includes failure percentage."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {f"Net{i}": i for i in range(1, 11)}
        # Route 7 out of 10 (30% failure rate)
        router = _make_mock_router(routed_nets=list(range(1, 8)), num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=10,
                quiet=False,
            )

        text = output.getvalue()
        assert "3/10 nets failed" in text
        assert "30% failure rate" in text

    def test_auto_layers_suggestion_without_pcb_file(self):
        """Suggestion works without pcb_file (no filename in command)."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=2,
                quiet=False,
            )

        text = output.getvalue()
        assert "kct route --auto-layers" in text

    def test_auto_layers_suggestion_with_pcb_file(self):
        """Suggestion includes pcb filename when provided."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=2,
                quiet=False,
                pcb_file="my_board.kicad_pcb",
            )

        text = output.getvalue()
        assert "kct route my_board.kicad_pcb --auto-layers" in text

    def test_auto_layers_suggestion_quiet_mode(self):
        """No output in quiet mode."""
        from kicad_tools.router.output import show_routing_summary

        net_map = {"A": 1, "B": 2}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=2,
                quiet=True,
            )

        text = output.getvalue()
        assert text == ""


class TestExportFailedNets:
    """Tests for --export-failed-nets feature."""

    def test_export_failed_nets_function(self, tmp_path):
        """_export_failed_nets writes correct net names to file."""
        from kicad_tools.cli.route_cmd import _export_failed_nets

        net_map = {"GND": 0, "VCC": 1, "SDA": 2, "SCL": 3, "NRST": 4}
        router = _make_mock_router(routed_nets=[1, 2])  # VCC and SDA routed

        export_file = tmp_path / "failed.txt"
        result = _export_failed_nets(router, net_map, str(export_file), quiet=True)

        assert result is True
        content = export_file.read_text()
        lines = content.strip().split("\n")
        # Net 3 (SCL) and Net 4 (NRST) are unrouted; GND (net 0) is excluded
        assert sorted(lines) == ["NRST", "SCL"]

    def test_export_failed_nets_no_failures(self, tmp_path):
        """_export_failed_nets returns False when all nets are routed."""
        from kicad_tools.cli.route_cmd import _export_failed_nets

        net_map = {"GND": 0, "VCC": 1, "SDA": 2}
        router = _make_mock_router(routed_nets=[1, 2])  # All signal nets routed

        export_file = tmp_path / "failed.txt"
        result = _export_failed_nets(router, net_map, str(export_file), quiet=True)

        assert result is False
        assert not export_file.exists()

    def test_export_failed_nets_writes_one_per_line(self, tmp_path):
        """Each failed net name is on its own line."""
        from kicad_tools.cli.route_cmd import _export_failed_nets

        net_map = {f"NET_{i}": i for i in range(1, 6)}
        router = _make_mock_router(routed_nets=[1])  # Only NET_1 routed

        export_file = tmp_path / "failed.txt"
        _export_failed_nets(router, net_map, str(export_file), quiet=True)

        content = export_file.read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 4  # NET_2, NET_3, NET_4, NET_5
        # Verify trailing newline
        assert content.endswith("\n")

    def test_export_failed_nets_cli_flag_in_help(self):
        """--export-failed-nets appears in help output."""
        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()
        assert "--export-failed-nets" in help_text

    def test_export_failed_nets_forwarded_by_run_route_command(self):
        """run_route_command forwards --export-failed-nets to route_cmd.main."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="0.25",
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
            auto_layers=False,
            max_layers=6,
            min_completion=0.95,
            adaptive_rules=False,
            min_trace=None,
            min_clearance_floor=None,
            manufacturer="jlcpcb",
            high_performance=False,
            skip_drc=False,
            auto_fix=False,
            auto_fix_passes=None,
            export_failed_nets="failed.txt",
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--export-failed-nets" in call_args
            idx = call_args.index("--export-failed-nets")
            assert call_args[idx + 1] == "failed.txt"

    def test_export_failed_nets_not_forwarded_when_none(self):
        """run_route_command does NOT forward --export-failed-nets when not set."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="0.25",
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
            auto_layers=False,
            max_layers=6,
            min_completion=0.95,
            adaptive_rules=False,
            min_trace=None,
            min_clearance_floor=None,
            manufacturer="jlcpcb",
            high_performance=False,
            skip_drc=False,
            auto_fix=False,
            auto_fix_passes=None,
            export_failed_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--export-failed-nets" not in call_args

    def test_export_failed_nets_in_parser(self):
        """--export-failed-nets is in the route subparser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        # Parse with route subcommand
        args = parser.parse_args(["route", "test.kicad_pcb", "--export-failed-nets", "out.txt"])
        assert args.export_failed_nets == "out.txt"


class TestPartialResultSaving:
    """Tests for saving partial results on clean partial exit."""

    def test_save_partial_results_called_on_partial_exit(self, tmp_path):
        """_save_partial_results is called when routing partially succeeds."""
        from kicad_tools.cli.route_cmd import _interrupt_state, _save_partial_results

        # Set up the interrupt state with valid router and paths
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20240101) (generator test))")

        output_file = tmp_path / "board_routed.kicad_pcb"

        mock_router = MagicMock()
        mock_router.routes = [_make_mock_route(1)]
        mock_router.to_sexp.return_value = (
            '(segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu") (net 1))'
        )
        mock_router.get_statistics.return_value = {
            "nets_routed": 1,
            "segments": 1,
            "vias": 0,
        }

        # Set global state
        _interrupt_state["router"] = mock_router
        _interrupt_state["output_path"] = output_file
        _interrupt_state["pcb_path"] = pcb_file
        _interrupt_state["quiet"] = True

        result = _save_partial_results()

        assert result is True
        partial_path = output_file.with_stem(output_file.stem + "_partial")
        assert partial_path.exists()
        content = partial_path.read_text()
        assert "(segment" in content

        # Clean up global state
        _interrupt_state["router"] = None
        _interrupt_state["output_path"] = None
        _interrupt_state["pcb_path"] = None

    def test_save_partial_no_routes(self, tmp_path):
        """_save_partial_results returns False when no routes exist."""
        from kicad_tools.cli.route_cmd import _interrupt_state, _save_partial_results

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20240101) (generator test))")

        output_file = tmp_path / "board_routed.kicad_pcb"

        mock_router = MagicMock()
        mock_router.routes = []  # No routes

        _interrupt_state["router"] = mock_router
        _interrupt_state["output_path"] = output_file
        _interrupt_state["pcb_path"] = pcb_file
        _interrupt_state["quiet"] = True

        result = _save_partial_results()

        assert result is False

        # Clean up
        _interrupt_state["router"] = None
        _interrupt_state["output_path"] = None
        _interrupt_state["pcb_path"] = None


class TestAutoLayersSuggestionInJSON:
    """Tests for --auto-layers suggestion in JSON diagnostics output."""

    def test_json_diagnostics_includes_layer_suggestion(self):
        """JSON diagnostics includes layer count suggestion for 2-layer boards."""
        from kicad_tools.router.output import get_routing_diagnostics_json

        net_map = {"A": 1, "B": 2, "C": 3}
        router = _make_mock_router(routed_nets=[1], num_layers=2)

        # Add a mock failure for congestion
        failure = MagicMock()
        failure.net = 2
        failure.net_name = "B"
        failure.failure_cause = SimpleNamespace(value="congestion")
        failure.source_pad = ("U1", "1")
        failure.target_pad = ("R1", "1")
        failure.source_coords = (0, 0)
        failure.target_coords = (1, 1)
        failure.blocking_components = []
        failure.blocking_nets = []
        failure.reason = "Area too crowded"
        failure.analysis = None
        router.routing_failures = [failure]

        result = get_routing_diagnostics_json(
            router, net_map, nets_to_route=3, current_strategy="basic"
        )

        # Verify the suggestions include a layer count suggestion
        suggestions = result.get("suggestions", [])
        layer_suggestions = [s for s in suggestions if s.get("category") == "LAYER_COUNT"]
        assert len(layer_suggestions) > 0
        assert "--layers 4" in layer_suggestions[0]["fix"]
