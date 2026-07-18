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


def _make_real_partial_router():
    """Build a router with real pads/nets/routes exercising the partial case.

    Nets:
    - VCC (1): two pads joined by a segment -> fully routed.
    - SCL (3): two pads, no segments -> fully unrouted.
    - NRST (4): three pads, a segment joining two of them; the third pad is
      far away and stranded -> partial (2/3, stranded R1.1).
    """
    from kicad_tools.router.primitives import Layer, Pad, Route, Segment

    def _pad(x, y, net, ref, pin):
        return Pad(
            x=x,
            y=y,
            width=0.5,
            height=0.5,
            net=net,
            net_name=f"NET{net}",
            ref=ref,
            pin=pin,
        )

    def _seg(x1, y1, x2, y2):
        return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU)

    pads = {
        ("U1", "1"): _pad(0.0, 0.0, 1, "U1", "1"),
        ("U2", "1"): _pad(5.0, 0.0, 1, "U2", "1"),
        ("U1", "2"): _pad(0.0, 10.0, 3, "U1", "2"),
        ("U2", "2"): _pad(5.0, 10.0, 3, "U2", "2"),
        ("U1", "3"): _pad(0.0, 5.0, 4, "U1", "3"),
        ("U2", "3"): _pad(5.0, 5.0, 4, "U2", "3"),
        ("R1", "1"): _pad(20.0, 20.0, 4, "R1", "1"),
    }
    nets = {
        1: [("U1", "1"), ("U2", "1")],
        3: [("U1", "2"), ("U2", "2")],
        4: [("U1", "3"), ("U2", "3"), ("R1", "1")],
    }
    routes = [
        Route(net=1, net_name="NET1", segments=[_seg(0.0, 0.0, 5.0, 0.0)]),
        Route(net=4, net_name="NET4", segments=[_seg(0.0, 5.0, 5.0, 5.0)]),
    ]
    return SimpleNamespace(pads=pads, nets=nets, routes=routes)


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
        assert "2/4 nets" in text
        assert "failed on 2 layers" in text

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
        assert "3/10 nets" in text
        assert "30%" in text
        assert "failed on 2 layers" in text

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

    def test_auto_layers_suggestion_suppressed_when_already_attempted(self):
        """Issue #2634: ``--auto-layers`` recommendation must not appear when
        the escalation loop has already run.

        Calling ``show_routing_summary`` with ``auto_layers_attempted=True``
        (the layer-escalation code path) should not tell the user to try
        ``--auto-layers``, because that's the path they're already on.
        """
        from kicad_tools.router.output import show_routing_summary

        net_map = {f"Net{i}": i for i in range(1, 5)}
        # 2 of 4 routed on 2 layers (>20% failure)
        router = _make_mock_router(routed_nets=[1, 2], num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=4,
                quiet=False,
                pcb_file="board.kicad_pcb",
                auto_layers_attempted=True,
            )

        text = output.getvalue()
        # The recommendation block still appears...
        assert "RECOMMENDATION" in text
        # ...but no longer says "Try: kct route ... --auto-layers"
        assert "kct route board.kicad_pcb --auto-layers" not in text
        # Helpful guidance is given instead
        assert "Auto-layer escalation already ran" in text

    def test_auto_layers_low_severity_tip_suppressed_when_already_attempted(self):
        """Lower-severity ``Try automatic layer escalation`` tip also suppressed.

        Same fix at the ``elif num_layers < 4`` branch (failure_pct <= 20).
        """
        from kicad_tools.router.output import show_routing_summary

        net_map = {f"Net{i}": i for i in range(1, 11)}
        # 9/10 routed on 2 layers => 10% failure (low severity branch)
        router = _make_mock_router(routed_nets=list(range(1, 10)), num_layers=2)

        output = StringIO()
        with patch("sys.stdout", output):
            show_routing_summary(
                router,
                net_map,
                nets_to_route=10,
                quiet=False,
                auto_layers_attempted=True,
            )

        text = output.getvalue()
        assert "Try automatic layer escalation" not in text


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

    def test_export_failed_nets_no_failures_still_writes_empty_file(self, tmp_path):
        """When there is nothing to report the file is still written (issue #4316).

        Callers rely on the export file existing whenever routing is
        incomplete, so the empty case emits a well-formed empty payload
        rather than skipping the write.
        """
        from kicad_tools.cli.route_cmd import _export_failed_nets

        net_map = {"GND": 0, "VCC": 1, "SDA": 2}
        router = _make_mock_router(routed_nets=[1, 2])  # All signal nets routed

        # Text path: empty file is created.
        text_file = tmp_path / "failed.txt"
        result = _export_failed_nets(router, net_map, str(text_file), quiet=True)
        assert result is True
        assert text_file.exists()
        assert text_file.read_text() == ""

        # JSON path: a well-formed empty array is written.
        import json

        json_file = tmp_path / "failed.json"
        result = _export_failed_nets(router, net_map, str(json_file), quiet=True)
        assert result is True
        assert json_file.exists()
        assert json.loads(json_file.read_text()) == []

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

    def test_export_failed_nets_filters_by_nets_to_route_ids(self, tmp_path):
        """When nets_to_route_ids is provided, only those nets appear in export."""
        from kicad_tools.cli.route_cmd import _export_failed_nets

        # net_map includes single-pad nets (5, 6) and power nets that are
        # NOT routing candidates, plus multi-pad signal nets (1-4).
        net_map = {
            "GND": 0,
            "VCC": 1,
            "SDA": 2,
            "SCL": 3,
            "NRST": 4,
            "SINGLE_PAD_A": 5,
            "SINGLE_PAD_B": 6,
        }
        # Only nets 1-4 are multi-pad signal nets targeted for routing
        multi_pad_net_ids = {1, 2, 3, 4}
        # Router successfully routed VCC(1) and SDA(2)
        router = _make_mock_router(routed_nets=[1, 2])

        export_file = tmp_path / "failed.txt"
        result = _export_failed_nets(
            router,
            net_map,
            str(export_file),
            quiet=True,
            nets_to_route_ids=multi_pad_net_ids,
        )

        assert result is True
        lines = export_file.read_text().strip().split("\n")
        # Only SCL(3) and NRST(4) should appear -- NOT SINGLE_PAD_A/B
        assert sorted(lines) == ["NRST", "SCL"]

    def test_export_failed_nets_without_filter_includes_all(self, tmp_path):
        """Without nets_to_route_ids, all non-GND unrouted nets appear (legacy)."""
        from kicad_tools.cli.route_cmd import _export_failed_nets

        net_map = {
            "GND": 0,
            "VCC": 1,
            "SDA": 2,
            "SINGLE_PAD": 3,
        }
        router = _make_mock_router(routed_nets=[1])

        export_file = tmp_path / "failed.txt"
        result = _export_failed_nets(
            router,
            net_map,
            str(export_file),
            quiet=True,
        )

        assert result is True
        lines = export_file.read_text().strip().split("\n")
        # Without filter, both SDA and SINGLE_PAD appear
        assert sorted(lines) == ["SDA", "SINGLE_PAD"]

    def test_export_failed_nets_json_includes_partial_nets(self, tmp_path):
        """.json export includes partial nets with stranded-pad detail (#4316)."""
        import json

        from kicad_tools.cli.route_cmd import _export_failed_nets

        router = _make_real_partial_router()
        net_map = {"GND": 0, "VCC": 1, "SCL": 3, "NRST": 4}

        export_file = tmp_path / "failed.json"
        result = _export_failed_nets(
            router,
            net_map,
            str(export_file),
            quiet=True,
            nets_to_route_ids={1, 3, 4},
        )

        assert result is True
        payload = json.loads(export_file.read_text())
        by_net = {entry["net"]: entry for entry in payload}

        # SCL has no segments -> unrouted, both pads stranded.
        assert by_net["SCL"]["status"] == "unrouted"
        assert by_net["SCL"]["connected_pads"] == 0
        assert by_net["SCL"]["total_pads"] == 2

        # NRST has segments but one stranded pad -> partial, matching stdout
        # "5/7"-style connected/total counts and naming the stranded pad.
        assert by_net["NRST"]["status"] == "partial"
        assert by_net["NRST"]["connected_pads"] == 2
        assert by_net["NRST"]["total_pads"] == 3
        assert by_net["NRST"]["stranded_pads"] == ["R1.1"]

        # VCC is fully routed and must NOT appear.
        assert "VCC" not in by_net

    def test_export_failed_nets_text_appends_partial_names(self, tmp_path):
        """Non-.json export keeps legacy one-per-line format, incl. partial nets (#4316)."""
        from kicad_tools.cli.route_cmd import _export_failed_nets

        router = _make_real_partial_router()
        net_map = {"GND": 0, "VCC": 1, "SCL": 3, "NRST": 4}

        export_file = tmp_path / "failed.txt"
        result = _export_failed_nets(
            router,
            net_map,
            str(export_file),
            quiet=True,
            nets_to_route_ids={1, 3, 4},
        )

        assert result is True
        lines = export_file.read_text().strip().split("\n")
        # Both the unrouted (SCL) and partial (NRST) nets appear; VCC does not.
        assert sorted(lines) == ["NRST", "SCL"]

    def test_export_failed_nets_dual_mode_by_extension(self, tmp_path):
        """.json yields structured JSON; other extensions yield plain text (#4316)."""
        import json

        from kicad_tools.cli.route_cmd import _export_failed_nets

        router = _make_real_partial_router()
        net_map = {"GND": 0, "VCC": 1, "SCL": 3, "NRST": 4}

        json_file = tmp_path / "out.json"
        _export_failed_nets(
            router, net_map, str(json_file), quiet=True, nets_to_route_ids={1, 3, 4}
        )
        # Parses as JSON (structured payload).
        assert isinstance(json.loads(json_file.read_text()), list)

        text_file = tmp_path / "out.lst"
        _export_failed_nets(
            router, net_map, str(text_file), quiet=True, nets_to_route_ids={1, 3, 4}
        )
        # Not JSON -- plain net names, one per line.
        content = text_file.read_text()
        with contextlib.suppress(json.JSONDecodeError):
            json.loads(content)
            raise AssertionError("non-.json path should not emit JSON")
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


class TestCongestionMapEmptyGuard:
    """Crash guard for get_congestion_map on an empty congestion grid (#4316)."""

    def _make_grid(self):
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import LayerStack
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        stack = LayerStack.four_layer_all_signal()
        return RoutingGrid(50, 50, rules, origin_x=0, origin_y=0, layer_stack=stack)

    def test_get_congestion_map_returns_zeros_on_empty(self):
        """A zero-size _congestion array yields zeros instead of raising.

        Reproduces the lattice-engine path where _congestion is never
        populated: np.max over an empty array previously raised
        "zero-size array to reduction operation maximum which has no identity".
        """
        import numpy as np

        grid = self._make_grid()
        # Simulate the lattice/mesh engine: an empty congestion grid.
        grid._congestion = np.empty((0, 0, 0), dtype=grid._congestion.dtype)

        stats = grid.get_congestion_map()

        assert stats == {
            "max_congestion": 0.0,
            "avg_congestion": 0.0,
            "congested_regions": 0,
        }

    def test_get_congestion_map_normal_grid_still_works(self):
        """A normally-populated grid still returns real statistics."""
        grid = self._make_grid()
        stats = grid.get_congestion_map()
        # Fresh grid: no congestion recorded, but the call must not raise.
        assert stats["max_congestion"] == 0.0
        assert stats["congested_regions"] == 0
