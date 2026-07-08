"""Regression tests for placement scoring parity across surfaces (issue #3940).

The three optimizer-facing scoring surfaces (`optimize-placement --dry-run`,
the optimizer's internal `_evaluate`, and a direct `evaluate_placement()` call)
must agree when scoring the *same* layout. The original defect was that
`--dry-run` scored a freshly generated force-directed seed instead of the
actual on-disk placement, so its ovl/drc were meaningless as a check.

These tests load the committed board-04 (STM32 devboard) layout and assert:

* `--dry-run` scores the layout as placed (feasible, ovl=0), NOT a random seed.
* `--dry-run` output matches a direct `evaluate_placement()` call on the
  decoded current placement vector within a tight tolerance.
* A board whose moveable footprints are all cleanly placed reports ovl=0/drc=0
  under `--dry-run`, matching the intent of `kct placement check`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.cli.optimize_placement_cmd import (
    _build_footprint_sizes,
    _evaluate,
    _parse_weights,
    _read_board_data,
    _read_current_vector,
    run_optimize_placement,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Board-04 committed layout: a clean, feasible hand placement used across the
# fleet-parity suite. Repo-relative from tests/placement/ -> repo root.
BOARD_04 = (
    Path(__file__).resolve().parents[2]
    / "boards"
    / "04-stm32-devboard"
    / "output"
    / "stm32_devboard.kicad_pcb"
)


@pytest.fixture
def board_04_path() -> Path:
    if not BOARD_04.exists():
        pytest.skip(f"board-04 fixture not found: {BOARD_04}")
    return BOARD_04


def _parse_dry_run_score(output: str) -> tuple[float, float, bool]:
    """Extract (overlap, drc, feasible) from `--dry-run` stdout.

    The score line looks like::

        Current: 493.8000 (feasible) [wl=351.00 ovl=0.00 bnd=0.00 drc=0 area=1428.00]
    """
    m = re.search(r"ovl=([\d.eE+-]+)\s+bnd=[\d.eE+-]+\s+drc=([\d.eE+-]+)", output)
    assert m is not None, f"could not parse ovl/drc from dry-run output:\n{output}"
    ovl = float(m.group(1))
    drc = float(m.group(2))
    feasible = "(feasible)" in output
    return ovl, drc, feasible


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dry_run_scores_current_layout_not_a_seed(board_04_path, capsys):
    """--dry-run must evaluate the on-disk placement (feasible), not a seed.

    Board-04's committed layout is a clean hand placement. Under the old
    (buggy) behaviour, --dry-run scored a force-directed seed and reported
    a wildly infeasible overlap/DRC. It must now report the actual layout,
    which is feasible with zero bbox overlap.
    """
    result = run_optimize_placement(str(board_04_path), dry_run=True)
    assert result == 0

    out = capsys.readouterr().out
    ovl, drc, feasible = _parse_dry_run_score(out)

    assert feasible, f"expected feasible on-disk layout, got:\n{out}"
    assert ovl == pytest.approx(0.0, abs=0.5)
    assert drc == pytest.approx(0.0, abs=1)


def test_dry_run_matches_direct_evaluate_placement(board_04_path, capsys):
    """--dry-run scoring agrees with a direct evaluate_placement() call.

    Both paths must score the same decoded current vector, so their overlap
    and DRC values agree within the documented tolerance (±0.5 overlap,
    ±1 drc).
    """
    # --- Direct path: decode current positions and evaluate ---
    components, nets, board_outline, rules, _origin = _read_board_data(str(board_04_path))
    footprint_sizes = _build_footprint_sizes(components)
    cost_config = _parse_weights(None)

    current_vector = _read_current_vector(str(board_04_path), components)
    direct_score = _evaluate(
        current_vector,
        components,
        nets,
        rules,
        board_outline,
        cost_config,
        footprint_sizes,
    )

    # --- CLI path: run --dry-run and parse its printed score ---
    result = run_optimize_placement(str(board_04_path), dry_run=True)
    assert result == 0
    out = capsys.readouterr().out
    cli_ovl, cli_drc, cli_feasible = _parse_dry_run_score(out)

    assert cli_ovl == pytest.approx(direct_score.breakdown.overlap, abs=0.5)
    assert cli_drc == pytest.approx(direct_score.breakdown.drc, abs=1)
    assert cli_feasible == direct_score.is_feasible


def test_read_current_vector_round_trips_positions(board_04_path):
    """The encoded current vector decodes back to the on-disk positions.

    This guards the core of the fix: _read_current_vector must faithfully
    capture the footprint positions (board-relative) so that decode() yields
    the same coordinates the writer/analyzer operate on.
    """
    from kicad_tools.placement.vector import decode
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    components, _nets, _board, _rules, _origin = _read_board_data(str(board_04_path))
    vector = _read_current_vector(str(board_04_path), components)
    placed = decode(vector, components)

    # Reference positions straight from the PCB (board-relative).
    pcb = SchemaPCB.load(str(board_04_path))
    ref_pos = {fp.reference: fp.position for fp in pcb.footprints if fp.reference}

    assert len(placed) == len(components)
    for p in placed:
        assert p.reference in ref_pos
        ex, ey = ref_pos[p.reference]
        assert p.x == pytest.approx(ex, abs=1e-6)
        assert p.y == pytest.approx(ey, abs=1e-6)


def test_dry_run_deterministic(board_04_path, capsys):
    """--dry-run on a fixed layout is deterministic (no random seed leak).

    Because it now scores the on-disk layout rather than a randomly seeded
    one, repeated runs must produce identical overlap/DRC numbers.
    """
    run_optimize_placement(str(board_04_path), dry_run=True)
    first = _parse_dry_run_score(capsys.readouterr().out)

    run_optimize_placement(str(board_04_path), dry_run=True)
    second = _parse_dry_run_score(capsys.readouterr().out)

    assert first == second
