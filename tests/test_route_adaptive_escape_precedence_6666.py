"""The dense-package escape pre-phase wins over the adaptive grid (Issue #6666).

``do_routing`` used to take the adaptive multi-resolution branch first and
return from it, so ``_should_use_escape_routing`` / ``Autorouter.route_with_escape``
never ran on a board with a multi-resolution plan.  That was latent until PR
#4877 let ``compute_multi_resolution_plan`` see KiCad 7+ refs; afterwards
chorus-test's U5/U8 escapes collapsed (30/51 -> 19/51).

These tests drive the real ``kct route`` single-attempt path on a small board,
force a multi-resolution plan, mock the dense-package detector, and stub both
routing entry points so only the branch selection is exercised.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.router.adaptive_grid import AdaptiveGridRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import FineZone, MultiResolutionGridPlan


def _plan() -> MultiResolutionGridPlan:
    zone = FineZone(
        ref="U1",
        x_min=0.0,
        y_min=0.0,
        x_max=10.0,
        y_max=10.0,
        resolution=0.05,
    )
    return MultiResolutionGridPlan(
        coarse_resolution=0.1,
        fine_zones=[zone],
        total_cell_estimate=1000,
        uniform_fallback=0.1,
    )


class _Dense:
    """Minimal stand-in for ``PackageInfo`` (only ``.ref`` is read)."""

    ref = "U1"


def _run(pcb: Path, tmp_path: Path, *, dense: bool, extra: list[str] | None = None):
    calls: list[str] = []

    def _escape(self, *args, **kwargs):
        calls.append("route_with_escape")
        return []

    def _adaptive(self, *args, **kwargs):
        calls.append("route_adaptive")
        raise AssertionError("stop after branch selection")

    detect_calls: list[int] = []

    def _detect(self):
        detect_calls.append(1)
        return [_Dense()] if dense else []

    with (
        patch(
            "kicad_tools.router.io.compute_multi_resolution_plan",
            return_value=_plan(),
        ),
        patch.object(Autorouter, "detect_dense_packages", _detect),
        patch.object(Autorouter, "route_with_escape", _escape),
        patch.object(AdaptiveGridRouter, "route_adaptive", _adaptive),
    ):
        route_cmd.main(
            [
                str(pcb),
                "-o",
                str(tmp_path / "routed.kicad_pcb"),
                "--grid",
                "auto",
                "--layers",
                "2",
                "--no-auto-layers",
                "--no-cache",
                "--skip-drc",
                "--allow-unsafe-grid",
                "--force",
                # No --timeout: a finite budget runs the route in a supervised
                # worker process, where these in-process patches don't apply.
                *(extra or []),
            ]
        )
    return calls, len(detect_calls)


def test_dense_package_takes_escape_prephase_over_adaptive(routing_test_pcb, tmp_path, capsys):
    calls, detects = _run(routing_test_pcb, tmp_path, dense=True)
    out = capsys.readouterr().out
    assert "route_with_escape" in calls
    assert "route_adaptive" not in calls
    assert "Escape routing: auto-enabled" in out
    assert "Using adaptive multi-resolution grid strategy" not in out
    assert "escape pre-phase takes precedence (#6666)" in out
    # The helper prints its verdict, so it is evaluated exactly once.
    assert out.count("Escape routing: auto-enabled") == 1
    assert detects == 1


def test_no_dense_package_keeps_adaptive_branch(routing_test_pcb, tmp_path, capsys):
    calls, detects = _run(routing_test_pcb, tmp_path, dense=False)
    out = capsys.readouterr().out
    assert "route_adaptive" in calls
    assert "route_with_escape" not in calls
    assert "Using adaptive multi-resolution grid strategy" in out
    assert "Escape routing: auto-enabled" not in out
    assert detects == 1


def test_no_escape_routing_flag_keeps_adaptive_branch(routing_test_pcb, tmp_path, capsys):
    """``--no-escape-routing`` opts out of the pre-phase, so adaptive still runs."""
    calls, detects = _run(routing_test_pcb, tmp_path, dense=True, extra=["--no-escape-routing"])
    out = capsys.readouterr().out
    assert "route_adaptive" in calls
    assert "route_with_escape" not in calls
    assert "Using adaptive multi-resolution grid strategy" in out
    assert detects == 0


@pytest.mark.parametrize("strategy", ["uniform", "adaptive"])
def test_grid_strategy_reaches_route_cmd_through_kct_route(strategy):
    """``kct route ... --grid-strategy X`` lands as ``args.grid_strategy == X``
    on the inner ``route_cmd`` parser (it used to die with "unrecognized
    arguments" because only ``route_cmd`` declared it)."""
    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser

    outer = create_parser().parse_args(
        ["route", "board.kicad_pcb", "--grid-strategy", strategy, "--dry-run"]
    )
    assert outer.grid_strategy == strategy
    with patch("kicad_tools.cli.route_cmd.main", return_value=0) as mock_main:
        run_route_command(outer)
    sub_argv = mock_main.call_args[0][0]
    if strategy == "adaptive":
        # Default: flag-off argv stays byte-identical.
        assert "--grid-strategy" not in sub_argv
    inner = route_cmd._route_parser().parse_args(sub_argv)
    assert inner.grid_strategy == strategy
