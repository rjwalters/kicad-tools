"""Issue #4242: expose ``--max-cells`` to override the auto-grid memory budget.

``auto_select_grid_resolution`` (and, on the default ``--grid-strategy
adaptive`` path, ``compute_multi_resolution_plan``) filters grid candidates by
a ``max_cells`` budget.  On large boards the hardcoded 500,000 default forces a
coarse, clearance-unsafe grid, which the #3911 safety gate then refuses.  This
flag lets the caller raise (or lower) that budget so ``--grid auto`` can reach
a safe, well-aligned grid.

The flag must:

* be declared on BOTH the standalone ``route_cmd.py`` parser and the unified
  ``cli/parser.py`` route subcommand (guards argparse drift -- see
  ``tests/test_cli_parser_drift.py``), defaulting to 500,000;
* be forwarded by ``run_route_command`` only when the user changed it from the
  default (flag-off argv byte-identical);
* thread through to BOTH the top-level ``auto_select_grid_resolution`` call and
  the ``compute_multi_resolution_plan`` call in ``route_cmd`` -- and
  ``compute_multi_resolution_plan`` must in turn forward it to its own internal
  ``auto_select_grid_resolution`` call (the dead-parameter bug this issue also
  fixes).
"""

from __future__ import annotations

import contextlib
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kicad_tools.cli import route_cmd
from kicad_tools.router.io import GridAutoSelection, PadPosition

# =============================================================================
# Flag definition + default (both parsers)
# =============================================================================


class TestFlagDefinedInBothParsers:
    def test_max_cells_in_route_cmd_help(self):
        """``route_cmd.main(['--help'])`` lists ``--max-cells``."""
        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()
        assert "--max-cells" in help_text
        # Help must point users hitting the 'Increase max_cells' message here.
        collapsed = " ".join(help_text.split())
        assert "max_cells" in collapsed

    def test_max_cells_in_unified_parser(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--max-cells", "16000000"])
        assert args.max_cells == 16_000_000

    def test_max_cells_default_in_unified_parser(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.max_cells == 500_000

    def test_max_cells_default_in_route_cmd_parser(self):
        """The inner ``route_cmd`` parser (built inline in ``main``) also
        declares ``--max-cells`` with a 500,000 default."""
        import argparse

        from kicad_tools.cli.route_cmd import main as route_main

        captured: dict[str, argparse.ArgumentParser] = {}
        real_parse_args = argparse.ArgumentParser.parse_args

        def fake_parse_args(self, *args, **kwargs):
            if getattr(self, "prog", "") == "kicad-tools route":
                captured["parser"] = self
                raise SystemExit(0)
            return real_parse_args(self, *args, **kwargs)

        with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
            with contextlib.suppress(SystemExit):
                route_main([])

        assert "parser" in captured, "failed to capture inner route parser"
        args = captured["parser"].parse_args(["test.kicad_pcb"])
        assert args.max_cells == 500_000

    def test_max_cells_accepts_below_default(self):
        """It's a genuine override, not just a raise-the-ceiling flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--max-cells", "1000"])
        assert args.max_cells == 1000


# =============================================================================
# Forwarding: run_route_command -> route_cmd.main
# =============================================================================


def _base_args(**overrides) -> SimpleNamespace:
    base: dict[str, object] = {
        "pcb": "test.kicad_pcb",
        "output": None,
        "strategy": "negotiated",
        "skip_nets": None,
        "grid": "auto",
        "max_cells": 500_000,
        "trace_width": 0.2,
        "clearance": 0.15,
        "via_drill": 0.3,
        "via_diameter": 0.6,
        "mc_trials": 10,
        "iterations": 15,
        "verbose": False,
        "dry_run": True,
        "quiet": True,
        "power_nets": None,
        "layers": "auto",
        "force": False,
        "no_optimize": False,
        "auto_layers": False,
        "max_layers": 6,
        "min_completion": 0.95,
        "adaptive_rules": False,
        "min_trace": None,
        "min_clearance_floor": None,
        "manufacturer": "jlcpcb",
        "high_performance": False,
        "skip_drc": False,
        "auto_fix": False,
        "auto_fix_passes": None,
        "export_failed_nets": None,
        "differential_pairs": False,
        "diffpair_spacing": None,
        "diffpair_max_delta": None,
        "length_match_diffpairs": False,
        "length_match_groups": False,
        "strict": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestForwarding:
    def test_max_cells_forwarded_when_non_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(max_cells=16_000_000)
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--max-cells" in call_args
            idx = call_args.index("--max-cells")
            assert call_args[idx + 1] == "16000000"

    def test_max_cells_not_forwarded_when_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(max_cells=500_000)
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--max-cells" not in call_args

    def test_max_cells_below_default_is_forwarded(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(max_cells=1000)
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--max-cells" in call_args
            idx = call_args.index("--max-cells")
            assert call_args[idx + 1] == "1000"


# =============================================================================
# End-to-end threading into the grid-selection call sites
# =============================================================================


def _safe_selection(resolution: float = 0.05) -> GridAutoSelection:
    """A clearance-safe selection so the #3911 gate never fires."""
    return GridAutoSelection(
        resolution=resolution,
        off_grid_pads=0,
        total_pads=2,
        off_grid_percentage=0.0,
        candidates_tried=[(resolution, 0)],
        memory_capped=False,
        uncapped_resolution=None,
        origin_offset=(0.0, 0.0),
        clearance_compliant_at_clearance_over_2=True,
        memory_budget_used=500_000,
        lattice_rescued=False,
        memory_forced_unsafe_grid=False,
    )


class _GateReached(Exception):
    """Sentinel raised just past the safety gate."""


def _run_main_capturing(tmp_path, extra_args, *, auto_mock, multi_mock):
    """Drive ``route_cmd.main`` to the gate with grid analysis mocked.

    ``auto_mock``/``multi_mock`` replace ``auto_select_grid_resolution`` and
    ``compute_multi_resolution_plan`` so their call kwargs can be inspected.
    """
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n")
    pads = [PadPosition(x=10.0, y=10.0), PadPosition(x=20.0, y=10.0)]

    def _sentinel(*_a, **_k):
        raise _GateReached

    with (
        patch("kicad_tools.router.io.extract_pad_positions", return_value=pads),
        patch("kicad_tools.router.io.extract_board_dimensions", return_value=(200.0, 200.0)),
        patch("kicad_tools.router.io.auto_select_grid_resolution", auto_mock),
        patch("kicad_tools.router.io.compute_multi_resolution_plan", multi_mock),
        patch("kicad_tools.router.io.load_pads_for_analysis", return_value=pads),
        patch.object(route_cmd, "_resolve_starting_layers", side_effect=_sentinel),
    ):
        with contextlib.suppress(_GateReached):
            route_cmd.main([str(pcb), "--quiet", *extra_args])


class TestThreadingIntoCallSites:
    def test_auto_select_receives_user_max_cells(self, tmp_path):
        auto_mock = MagicMock(return_value=_safe_selection())
        multi_mock = MagicMock(return_value=None)
        _run_main_capturing(
            tmp_path,
            ["--max-cells", "16000000"],
            auto_mock=auto_mock,
            multi_mock=multi_mock,
        )
        assert auto_mock.call_args is not None
        assert auto_mock.call_args.kwargs.get("max_cells") == 16_000_000

    def test_multi_resolution_plan_receives_user_max_cells(self, tmp_path):
        """Default strategy is adaptive -> compute_multi_resolution_plan runs."""
        auto_mock = MagicMock(return_value=_safe_selection())
        multi_mock = MagicMock(return_value=None)
        _run_main_capturing(
            tmp_path,
            ["--max-cells", "16000000"],
            auto_mock=auto_mock,
            multi_mock=multi_mock,
        )
        assert multi_mock.call_args is not None
        assert multi_mock.call_args.kwargs.get("max_cells") == 16_000_000

    def test_default_max_cells_threaded_as_500k(self, tmp_path):
        """Flag omitted -> both call sites receive the 500,000 default."""
        auto_mock = MagicMock(return_value=_safe_selection())
        multi_mock = MagicMock(return_value=None)
        _run_main_capturing(tmp_path, [], auto_mock=auto_mock, multi_mock=multi_mock)
        assert auto_mock.call_args.kwargs.get("max_cells") == 500_000
        assert multi_mock.call_args.kwargs.get("max_cells") == 500_000

    def test_explicit_grid_ignores_max_cells(self, tmp_path):
        """With an explicit --grid value, the auto-grid path (and thus
        --max-cells) is never exercised -- no crash, no grid analysis."""
        auto_mock = MagicMock(return_value=_safe_selection())
        multi_mock = MagicMock(return_value=None)
        _run_main_capturing(
            tmp_path,
            ["--grid", "0.1", "--max-cells", "16000000"],
            auto_mock=auto_mock,
            multi_mock=multi_mock,
        )
        # Explicit grid short-circuits the auto path entirely.
        assert auto_mock.call_args is None
        assert multi_mock.call_args is None


# =============================================================================
# Regression guard: compute_multi_resolution_plan forwards its own max_cells
# =============================================================================


class TestDeadParameterFix:
    """Directly prove the previously-dead ``max_cells`` parameter of
    ``compute_multi_resolution_plan`` now reaches its internal
    ``auto_select_grid_resolution`` call."""

    def test_internal_uniform_call_receives_max_cells(self):
        from kicad_tools.router.io import compute_multi_resolution_plan

        pads = [PadPosition(x=10.0, y=10.0), PadPosition(x=20.0, y=10.0)]
        auto_mock = MagicMock(return_value=_safe_selection())
        with patch("kicad_tools.router.io.auto_select_grid_resolution", auto_mock):
            compute_multi_resolution_plan(
                pads=pads,
                clearance=0.1,
                board_width=200.0,
                board_height=200.0,
                max_cells=12345,
            )
        assert auto_mock.call_args is not None
        assert auto_mock.call_args.kwargs.get("max_cells") == 12345

    def test_default_max_cells_is_500k(self):
        """The function default aligns with the previously-effective 500k
        (the old 2,000,000 default was dead code -- never forwarded)."""
        from kicad_tools.router.io import compute_multi_resolution_plan

        pads = [PadPosition(x=10.0, y=10.0), PadPosition(x=20.0, y=10.0)]
        auto_mock = MagicMock(return_value=_safe_selection())
        with patch("kicad_tools.router.io.auto_select_grid_resolution", auto_mock):
            compute_multi_resolution_plan(
                pads=pads,
                clearance=0.1,
                board_width=200.0,
                board_height=200.0,
            )
        assert auto_mock.call_args.kwargs.get("max_cells") == 500_000
