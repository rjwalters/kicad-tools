"""Boards 05 and 04 lattice routes stay complete and manufacturable (#6449, #6451).

Board 05 was the first board to meet the full bar on a fresh route: every
signal net routed, 0 errors from both kicad-cli and ``kct check``, ``kct route``
exit 0.  #6427 then grew the lattice's static pad masks by the router's own
class clearances, which walled ``GND_SENSE`` out of ``U2.6`` (32/33, exit 3)
without any fast test noticing.  The structural cause is pinned by
``tests/router/test_lattice_pad_mask_designer_floors_6449.py``; this test is
the end-to-end assertion.  Board 04 (``--layers 2``, 9/9) regressed the same
way (Issue #6451: ``OSC_IN`` / ``OSC_OUT`` / ``SWDIO`` unrouted, exit 2).

It runs the issue's exact protocol through
``scripts/research/expand_route_experiment.py``.  Board 05 takes 15-25
minutes (board 04 a few), so the test is opt-in: set ``KCT_LATTICE_BOARD_GUARD=1`` (and build the native backend
first, ``uv run kct build-native``)::

    KCT_LATTICE_BOARD_GUARD=1 uv run pytest \\
        tests/test_lattice_boards_manufacturable_6449.py --no-cov -s

Run it before merging anything that touches lattice search geometry, pad
keep-outs, clearance floors or plane-via reservation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "scripts" / "research" / "expand_route_experiment.py"

#: Each issue's protocol, verbatim: (board, ``--extra`` flags, expected tally).
CASES = [
    pytest.param(
        "05",
        "--route-engine lattice --strategy basic --layers 4 --no-auto-layers "
        "--deterministic-budget --per-net-iterations 1000000 "
        "--no-placement-delta-feedback --seed 42 --no-cache",
        "33/33",
        id="board05-4L-6449",
    ),
    pytest.param(
        "04",
        "--route-engine lattice --strategy basic --layers 2 --no-auto-layers "
        "--deterministic-budget --per-net-iterations 1000000 "
        "--no-placement-delta-feedback --no-cache",
        "9/9",
        id="board04-2L-6451",
    ),
]

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("KCT_LATTICE_BOARD_GUARD") != "1",
        reason="opt-in board routes (15-25 min for board 05); set KCT_LATTICE_BOARD_GUARD=1",
    ),
]


@pytest.mark.parametrize("board,extra,tally", CASES)
def test_lattice_route_is_complete_and_manufacturable(tmp_path, board, extra, tally):
    rows_path = tmp_path / "rows.json"
    cmd = [
        sys.executable,
        str(HARNESS),
        "route",
        "--boards",
        board,
        "--grid-policy",
        "area",
        "--scales",
        "1.0",
        "--modes",
        "uniform",
        "--seed",
        "42",
        "--timeout",
        "7200",
        f"--extra={extra}",
        "--work-dir",
        str(tmp_path / "work"),
        "--json",
        str(rows_path),
    ]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=7700)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    rows = json.loads(rows_path.read_text())["rows"]
    assert len(rows) == 1, rows
    row = rows[0]
    summary = {
        k: row.get(k)
        for k in (
            "router_nets",
            "exit_code",
            "signal_nets_unfinished",
            "kct_check_errors",
            "kicad_drc_errors",
            "kicad_drc_unconnected",
            "manufacturable",
        )
    }
    assert row["router_nets"] == tally, summary
    assert row["exit_code"] == 0, summary
    assert row["signal_nets_unfinished"] == 0, summary
    assert row["kct_check_errors"] == 0, summary
    assert row["kicad_drc_errors"] == 0, summary
    assert row["manufacturable"] is True, summary
