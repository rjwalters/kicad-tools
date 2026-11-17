"""Analytical ``route --dry-run`` grid/cell/budget plan (Issue #4263).

A plain ``route --dry-run`` used to construct the full ``RoutingGrid`` (inside
``load_pcb_for_routing``) before it could report anything, so on a large board
it ran >45s and got OOM-killed -- exactly when the user is trying to discover
whether the board fits the memory budget.  The fix short-circuits ``--dry-run``
*before* the load and computes the verdict analytically.

These tests pin the two load-bearing guarantees:
  1. The analytical plan is returned WITHOUT constructing a ``RoutingGrid``
     (patched-to-explode grid ctor + load path), quickly, and cannot OOM.
  2. The plan composes with ``--max-cells`` and an explicit ``--grid``.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli import route_cmd


def _write_large_board(path: Path, width: float = 160.0, height: float = 100.0) -> Path:
    """Write a 160x100mm board whose fine 0.127mm grid would OOM the dry-run.

    At 0.127mm on a 160x100 board a 4-layer uniform grid is ~4M cells; the
    pre-#4263 dry-run allocated exactly that before it could print anything.
    """
    footprints = []
    for gx in range(10, int(width) - 5, 15):
        for gy in range(10, int(height) - 5, 15):
            pads = "\n".join(
                f'    (pad "{i + 1}" smd rect (at {-1.905 + i * 1.27:.3f} 0) '
                f'(size 0.6 1.5) (layers "F.Cu"))'
                for i in range(4)
            )
            footprints.append(f'  (footprint "SOIC" (layer "F.Cu") (at {gx} {gy})\n{pads}\n  )')
    board = (
        "(kicad_pcb (version 20221018) (generator test)\n"
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        f"  (gr_rect (start 0 0) (end {width:g} {height:g}) "
        '(layer "Edge.Cuts") (width 0.1))\n' + "\n".join(footprints) + "\n)\n"
    )
    path.write_text(board)
    return path


# ---------------------------------------------------------------------------
# The pure analytical helper.
# ---------------------------------------------------------------------------


def test_compute_plan_is_allocation_free_and_correct(tmp_path):
    """The plan math matches the canonical cols/rows/cells + 18 bytes/cell."""
    pcb = _write_large_board(tmp_path / "big.kicad_pcb")

    plan = route_cmd.compute_dry_run_grid_plan(
        pcb_path=pcb,
        selected_grid=0.127,
        clearance=0.15,
        num_layers=4,
        max_cells=500_000,
    )
    assert plan is not None
    assert plan.board_width == pytest.approx(160.0)
    assert plan.board_height == pytest.approx(100.0)
    # Canonical grid dims: int(w/res)+1, int(h/res)+1 (grid.py:739-740).
    assert plan.cols == int(160.0 / 0.127) + 1
    assert plan.rows == int(100.0 / 0.127) + 1
    assert plan.cells == plan.cols * plan.rows * 4
    # estimate_memory_bytes == cells * 18 (backend.py:774).
    assert plan.est_memory_bytes == plan.cols * plan.rows * 4 * 18
    # ~4M cells against a 500k budget -> exceeds.
    assert plan.cells > 500_000
    assert plan.fits is False
    # Finer/coarser lattice candidates are reported.
    assert len(plan.candidates) >= 2
    resolutions = [c.resolution for c in plan.candidates]
    assert 0.127 in resolutions  # the selected grid brackets the list


def test_plan_composes_with_max_cells(tmp_path):
    """The budget verdict flips when --max-cells is raised past the cell count."""
    pcb = _write_large_board(tmp_path / "big.kicad_pcb")

    tight = route_cmd.compute_dry_run_grid_plan(
        pcb_path=pcb, selected_grid=0.127, clearance=0.15, num_layers=4, max_cells=500_000
    )
    roomy = route_cmd.compute_dry_run_grid_plan(
        pcb_path=pcb, selected_grid=0.127, clearance=0.15, num_layers=4, max_cells=5_000_000
    )
    assert tight is not None and roomy is not None
    assert tight.cells == roomy.cells  # same board/grid -> same cell count
    assert tight.fits is False
    assert roomy.fits is True
    assert roomy.max_cells == 5_000_000


def test_plan_honors_explicit_grid(tmp_path):
    """An explicit --grid drives the selected resolution, not the auto pick."""
    pcb = _write_large_board(tmp_path / "big.kicad_pcb")

    plan = route_cmd.compute_dry_run_grid_plan(
        pcb_path=pcb, selected_grid=0.1, clearance=0.15, num_layers=2, max_cells=500_000
    )
    assert plan is not None
    assert plan.resolution == 0.1
    assert plan.num_layers == 2
    assert plan.cols == int(160.0 / 0.1) + 1
    # Rendering is stable and mentions the verdict.
    text = route_cmd.format_dry_run_grid_plan(plan)
    assert "Analytical Grid Plan" in text
    assert "0.1mm" in text


def test_plan_returns_none_without_board_outline(tmp_path):
    """No detectable outline -> None so the caller falls through to the load."""
    pcb = tmp_path / "no_outline.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018) (generator test))\n")
    plan = route_cmd.compute_dry_run_grid_plan(
        pcb_path=pcb, selected_grid=0.127, clearance=0.15, num_layers=4, max_cells=500_000
    )
    assert plan is None


# ---------------------------------------------------------------------------
# End-to-end CLI: --dry-run must NOT build the grid.
# ---------------------------------------------------------------------------


def test_dry_run_cli_builds_no_grid_and_reports_quickly(tmp_path, capsys):
    """``route --dry-run`` on a would-OOM board: analytical plan, no grid, fast.

    Both the load path and the ``RoutingGrid`` constructor are patched to
    explode; reaching either would mean the dry-run tried to allocate the very
    grid whose feasibility is in question (the OOM #4263 fixes).
    """
    pcb = _write_large_board(tmp_path / "big.kicad_pcb")

    def _explode_load(*a, **k):
        raise AssertionError("load_pcb_for_routing must not run for a plain --dry-run")

    def _explode_grid(*a, **k):
        raise AssertionError("RoutingGrid must not be constructed for a plain --dry-run")

    with (
        patch("kicad_tools.router.io.load_pcb_for_routing", side_effect=_explode_load),
        patch("kicad_tools.router.grid.RoutingGrid.__init__", side_effect=_explode_grid),
    ):
        start = time.perf_counter()
        rc = route_cmd.main(
            [
                str(pcb),
                "--dry-run",
                "--grid",
                "0.127",
                "--layers",
                "4",
                "--max-cells",
                "500000",
            ]
        )
        elapsed = time.perf_counter() - start

    assert rc == 0
    # No grid allocation means it is near-instant; generous bound for slow CI.
    assert elapsed < 5.0

    out = capsys.readouterr().out
    assert "Analytical Grid Plan" in out
    assert "Selected grid:  0.127mm" in out
    assert "cells" in out
    assert "MB" in out
    assert "EXCEEDS" in out  # ~4M cells vs a 500k budget
    assert "max-cells=500,000" in out


def test_dry_run_cli_fits_verdict(tmp_path, capsys):
    """A roomy --max-cells yields a FITS verdict via the same analytical path."""
    pcb = _write_large_board(tmp_path / "big.kicad_pcb")

    with patch(
        "kicad_tools.router.io.load_pcb_for_routing",
        side_effect=AssertionError("must not load for --dry-run"),
    ):
        rc = route_cmd.main(
            [
                str(pcb),
                "--dry-run",
                "--grid",
                "0.127",
                "--layers",
                "4",
                "--max-cells",
                "5000000",
            ]
        )

    assert rc == 0
    out = capsys.readouterr().out
    assert "FITS" in out
    assert "max-cells=5,000,000" in out
